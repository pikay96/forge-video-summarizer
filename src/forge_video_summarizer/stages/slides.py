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
import io
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from ..config import Config
from ._openai import call_responses_vision, make_client
from .frames import SlideCandidate, apply_cleanup, format_ts

log = logging.getLogger(__name__)

__all__ = [
    "place_slides",
    "SLIDE_PLACEHOLDER_RE",
    "select_slide_placements",
    "clean_selected_frames",
]

# Frames are downscaled before being sent to the model. Selection/region judgments need
# legible layout, not full resolution — and a dozen-plus full-size PNGs make the request
# payload huge (21 frames of a 914x720 talk = ~6.5 MB base64, which stalls the API call).
_VISION_MAX_WIDTH = 800
_VISION_JPEG_QUALITY = 70

# Matches an inserted placeholder line: ![slide@MM:SS] or ![slide@HH:MM:SS]
SLIDE_PLACEHOLDER_RE = re.compile(r"!\[slide@(\d{1,2}:\d{2}(?::\d{2})?)\]")
# Any [MM:SS] / [HH:MM:SS] anchor, wherever it appears (headings OR inline prose).
_ANCHOR_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
# Markdown heading line (used to place a slide at a section boundary when possible).
_HEADING_RE = re.compile(r"^#{2,4}\s")

_SELECT_INSTRUCTIONS = """\
You are curating slide screenshots for a video summary. You are shown, in order, several
candidate frames captured from a slide-style talk, each labeled with its timestamp. You are
also given the summary's timestamp anchors.

TASK 1 — SELECT. Keep ONLY the frames that are genuinely useful KEY slides: ones showing a
diagram, formula, table, comparison, or structured content that materially helps a reader.
DROP: near-duplicates of a slide you already kept (keep the single clearest/most complete
one), transition/blurred/half-drawn frames, frames that are mostly the speaker's webcam, and
near-empty slides. There is no target count — keep as few or as many as truly earn their
place, but do NOT miss a slide that presents an important concept or comparison.

TASK 2 — PLACE. For each kept slide, choose the anchor it best illustrates (the point in the
summary whose topic the slide depicts; usually at or just before the slide's timestamp).

TASK 3 — CLEAN. For each kept slide, report the regions that are NOT slide content:
  "chrome_bottom": fraction of the frame HEIGHT (0-1) where top browser/app UI ends — tabs,
      URL bar, window buttons. Use 0 when the frame has none.
  "webcam": [x0, x1, y0, y1] as fractions of width/height for the presenter's camera bubble.
      It may be in ANY corner and differs per frame. Use null when no webcam is visible.
      Give a tight box around the bubble only; do not include slide content.

Return ONLY a JSON array, no prose, no code fences:
[{"slide": "MM:SS", "section": "MM:SS", "chrome_bottom": 0.0, "webcam": [x0,x1,y0,y1]|null}]
Use the EXACT timestamp labels given for slides and the EXACT anchors given for sections.
If none of the frames are worth keeping, return [].
"""


def _data_url(path: Path) -> str | None:
    """Base64 data URL of the frame, downscaled to keep the request payload sane.

    IMPORTANT: the model reports cleanup regions as FRACTIONS, so downscaling does not
    affect how those map back onto the full-resolution frame we actually embed.
    Falls back to the original bytes if Pillow is unavailable.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - optional dependency
        return _raw_data_url(path)
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            if im.width > _VISION_MAX_WIDTH:
                ratio = _VISION_MAX_WIDTH / im.width
                im = im.resize((_VISION_MAX_WIDTH, max(1, int(im.height * ratio))))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=_VISION_JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001 - fall back to the original file
        return _raw_data_url(path)


def _raw_data_url(path: Path) -> str | None:
    try:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None


def _section_anchors(summary_markdown: str) -> list[str]:
    """Every timestamp anchor in the summary, in order, de-duped.

    With the concept-organized Walkthrough, anchors live INLINE in prose rather than in
    section headings, so we collect them wherever they appear.
    """
    seen: dict[str, None] = {}
    for m in _ANCHOR_RE.finditer(summary_markdown):
        seen.setdefault(m.group(1), None)
    return list(seen)


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
    and de-dupes so each slide is placed at most once. Cleanup fields are optional and
    sanitized: chrome_bottom clamped to [0,1), webcam kept only if 4 sane fractions."""
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
        if slide not in valid_slides or section not in valid_sections or slide in seen:
            continue
        seen.add(slide)
        out.append({
            "slide": slide,
            "section": section,
            "chrome_bottom": _clean_fraction(item.get("chrome_bottom")),
            "webcam": _clean_box(item.get("webcam")),
        })
    return out


def _clean_fraction(value: object) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return f if 0.0 <= f < 1.0 else 0.0


def _clean_box(value: object) -> tuple[float, float, float, float] | None:
    """Accept [x0,x1,y0,y1] fractions only if they form a sane, in-bounds box."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, x1, y0, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    return (x0, x1, y0, y1)


def clean_selected_frames(candidates: list[SlideCandidate], placements: list[dict]) -> None:
    """Apply the model-reported cleanup (webcam mask + chrome crop) to the kept frames.

    Only frames that were actually selected are touched — no point cleaning discards.
    Best-effort per frame; a failure leaves that frame as-is.
    """
    by_label = {format_ts(c.timestamp): c.path for c in candidates}
    for p in placements:
        path = by_label.get(p["slide"])
        if path is None or not path.is_file():
            continue
        if p.get("chrome_bottom") or p.get("webcam"):
            apply_cleanup(
                path,
                chrome_bottom=p.get("chrome_bottom", 0.0),
                webcam=p.get("webcam"),
            )


def place_slides(summary_markdown: str, placements: list[dict]) -> str:
    """Insert `![slide@MM:SS]` placeholder lines next to the anchor each slide illustrates.

    Anchors now live inline in the prose (concept-organized Walkthrough), so a slide is
    inserted after the LINE that first mentions its target anchor — i.e. right below the
    paragraph that makes the point the slide depicts. Prose is never modified, only
    placeholder lines are added. A slide whose anchor cannot be found is skipped.
    """
    if not placements:
        return summary_markdown
    by_anchor: dict[str, list[str]] = {}
    for p in placements:
        by_anchor.setdefault(p["section"], []).append(p["slide"])

    lines = summary_markdown.splitlines()
    out_lines: list[str] = []
    placed: set[str] = set()
    for line in lines:
        out_lines.append(line)
        if _HEADING_RE.match(line):
            continue  # never insert between a heading and its first paragraph
        for anchor in _ANCHOR_RE.findall(line):
            if anchor in by_anchor and anchor not in placed:
                placed.add(anchor)
                for slide_ts in by_anchor[anchor]:
                    out_lines.append("")
                    out_lines.append(f"![slide@{slide_ts}]")
    return "\n".join(out_lines)
