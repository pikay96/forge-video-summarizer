"""Stage 4b — Slide selection & placement (optional, vision see-then-write).

Given the slide-frame candidates (from `frames.py`) and the finished summary, ask the
vision model to decide which candidates are genuinely KEY slides worth embedding, and
which Walkthrough section (`### [MM:SS] ...` heading) each belongs under. The model SEES
the actual frames, so it can drop transition/blur/near-duplicate frames and keep only the
informative ones (no fixed cap — its judgment).

We keep the model's job narrow and its output STRUCTURED (JSON): it returns
`[{"slide": "<MM:SS>", "section": "<MM:SS>"}]`, referencing slide labels + section anchors
we gave it. We then DETERMINISTICALLY insert `![slide@<MM:SS>]` placeholder lines right
after the matching section heading — the prose is never rewritten by the model, only
annotated. Export (`export_notion`) resolves those placeholders to uploaded images.

Best-effort throughout: any failure returns the summary unchanged (slides are additive).
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from ..config import Config
from ._openai import call_responses_vision, make_client
from .frames import SlideCandidate, format_ts

log = logging.getLogger(__name__)

__all__ = ["place_slides", "SLIDE_PLACEHOLDER_RE", "select_slide_placements"]

# Matches an inserted placeholder line: ![slide@MM:SS] or ![slide@HH:MM:SS]
SLIDE_PLACEHOLDER_RE = re.compile(r"!\[slide@(\d{1,2}:\d{2}(?::\d{2})?)\]")
# Section headings in the summary: "### [MM:SS] Title"
_SECTION_RE = re.compile(r"^(#{2,4})\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]")

_SELECT_INSTRUCTIONS = """\
You are curating slide screenshots for a video summary. You are shown, in order, several
candidate frames captured from a slide-style talk, each labeled with its timestamp. You are
also given the summary's Walkthrough section anchors.

Select ONLY the frames that are genuinely useful KEY slides — ones that show a diagram,
formula, table, or structured content that materially helps a reader understand a point.
DROP: near-duplicates of a slide you already kept (keep the single clearest one),
transition/blurred frames, frames that are mostly the speaker's webcam or a near-empty
slide. There is no target count — keep as few or as many as truly earn their place.

For each kept slide, choose the Walkthrough section anchor it best illustrates (the section
whose topic the slide depicts; usually the section at or just before the slide's timestamp).

Return ONLY a JSON array, no prose, no code fences:
[{"slide": "MM:SS", "section": "MM:SS"}, ...]
Use the EXACT timestamp labels given for slides and the EXACT anchors given for sections.
If none of the frames are worth keeping, return [].
"""


def _data_url(path: Path) -> str | None:
    try:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None


def _section_anchors(summary_markdown: str) -> list[str]:
    matches = (_SECTION_RE.match(ln) for ln in summary_markdown.splitlines())
    return [m.group(2) for m in matches if m]


def select_slide_placements(
    candidates: list[SlideCandidate],
    summary_markdown: str,
    config: Config,
    *,
    client: Any | None = None,
) -> list[dict]:
    """Vision call → list of {"slide": MM:SS, "section": MM:SS} placements (validated).

    Returns [] on any failure or if the model keeps nothing.
    """
    anchors = _section_anchors(summary_markdown)
    if not candidates or not anchors:
        return []
    client = client or make_client(config)

    images: list[tuple[str, str]] = []
    slide_labels: set[str] = set()
    for c in candidates:
        url = _data_url(c.path)
        if url is None:
            continue
        label = format_ts(c.timestamp)
        slide_labels.add(label)
        images.append((f"SLIDE {label}", url))
    if not images:
        return []

    text = (
        "Walkthrough section anchors (choose from these for 'section'):\n"
        + ", ".join(anchors)
        + f"\n\nThere are {len(images)} candidate slide frames below, in order."
    )
    try:
        reply = call_responses_vision(client, config, _SELECT_INSTRUCTIONS, text, images)
    except Exception as exc:  # noqa: BLE001 - slides are additive; degrade
        log.warning("slide selection vision call failed: %s", exc)
        return []

    placements = _parse_placements(reply, slide_labels, set(anchors))
    log.info("slide selection: %d candidates -> %d kept", len(candidates), len(placements))
    return placements


def _parse_placements(reply: str, valid_slides: set[str], valid_sections: set[str]) -> list[dict]:
    """Parse + validate the model's JSON. Drops entries with unknown slide/section labels
    and de-dupes so each slide is placed at most once."""
    raw = reply.strip()
    if "```" in raw:  # strip an accidental code fence
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        slide, section = item.get("slide"), item.get("section")
        if slide in valid_slides and section in valid_sections and slide not in seen:
            seen.add(slide)
            out.append({"slide": slide, "section": section})
    return out


def place_slides(summary_markdown: str, placements: list[dict]) -> str:
    """Insert `![slide@MM:SS]` placeholder lines after each target section heading.

    Idempotent-ish: placeholders are only inserted, prose is untouched. Multiple slides
    on the same section stack in order right under that heading.
    """
    if not placements:
        return summary_markdown
    by_section: dict[str, list[str]] = {}
    for p in placements:
        by_section.setdefault(p["section"], []).append(p["slide"])

    out_lines: list[str] = []
    for line in summary_markdown.splitlines():
        out_lines.append(line)
        m = _SECTION_RE.match(line)
        if m and m.group(2) in by_section:
            for slide_ts in by_section[m.group(2)]:
                out_lines.append("")
                out_lines.append(f"![slide@{slide_ts}]")
    return "\n".join(out_lines)
