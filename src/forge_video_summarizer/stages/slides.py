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
from .frames import SlideCandidate, _caption_score, format_ts

log = logging.getLogger(__name__)

__all__ = [
    "place_slides",
    "SLIDE_PLACEHOLDER_RE",
    "select_slide_placements",
]

# Frames are downscaled before being sent to the model. Selection/region judgments need
# legible layout, not full resolution — and a dozen-plus full-size PNGs make the request
# payload huge (21 frames of a 914x720 talk = ~6.5 MB base64, which stalls the API call).
_VISION_MAX_WIDTH = 800
_VISION_JPEG_QUALITY = 70
# Candidates per selection request. With 20+ images in one call the model under-attends and
# silently drops good slides (verified: it rejected a complete MHA/MQA/GQA comparison in a
# 21-image batch, then said "KEEP: Yes" for the very same frame shown alone).
_SELECT_BATCH = 6
# Post-selection quality correction: a kept frame may be swapped for a nearby candidate
# that shows the slide better (less caption occlusion, less edge cropping). The window is
# wide because a talk lingers on one slide while zooming/re-captioning it; the margin
# avoids churn on noise-level differences.
_DUP_WINDOW_SECONDS = 20.0
_DUP_MARGIN = 0.01
# Weight on edge-cropping relative to caption occlusion in the combined frame penalty.
# A cropped slide loses information permanently, so it must outweigh a caption that may
# well be sitting over empty margin.
_CROP_WEIGHT = 0.5

# Matches an inserted placeholder line: ![slide@MM:SS] or ![slide@HH:MM:SS]
SLIDE_PLACEHOLDER_RE = re.compile(r"!\[slide@(\d{1,2}:\d{2}(?::\d{2})?)\]")
# Any [MM:SS] / [HH:MM:SS] anchor, wherever it appears (headings OR inline prose).
_ANCHOR_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
# Markdown heading line (used to place a slide at a section boundary when possible).
_HEADING_RE = re.compile(r"^#{2,4}\s")
# The Walkthrough heading — screenshots are confined to this section.
_WALKTHROUGH_RE = re.compile(r"walkthrough|讲解|逐段|正文", re.IGNORECASE)

_SELECT_INSTRUCTIONS = """\
You are curating slide screenshots for a video summary. You are shown, in order, several
candidate frames captured from a slide-style talk, each labeled with its timestamp. You are
also given the summary's timestamp anchors.

TASK 1 — SELECT. Keep ONLY the frames that are genuinely useful KEY slides: ones showing a
diagram, formula, table, comparison, or structured content that materially helps a reader.
DROP: transition/blurred/half-drawn frames, frames that are mostly the speaker's webcam, and
near-empty slides. There is no target count — keep as few or as many as truly earn their
place, but do NOT miss a slide that presents an important concept or comparison.

DE-DUPLICATE BY KEEPING ONE, NEVER BY DROPPING ALL. When several frames show the SAME slide,
choose exactly ONE and drop the others. Dropping every frame of a slide because they look
repetitive is a serious error: that slide then appears nowhere in the summary. Before you
finish, re-check that every distinct slide topic in the candidates is represented by exactly
one kept frame.

WHICH DUPLICATE TO KEEP — this matters as much as which slide. Prefer, in order:
  1. the frame showing the MOST of the slide — nothing cut off at the left/right/top/bottom
     edges. Recordings zoom and pan, so the same slide may appear both cropped and complete;
     always take the complete one.
  2. the frame whose subtitle caption hides the least. Each frame is labelled with a
     "caption" score (0.0000 = no caption at all; higher = a caption covering more, and more
     important, slide content). Given two otherwise equal frames, take the lower score.
  3. the frame that is fully drawn rather than mid-animation.
Frames are embedded exactly as captured — nothing is cropped or masked afterwards — so a
frame that is cut off or has a caption over a formula stays that way in the summary. Choose
accordingly. Only if EVERY frame of an important slide is flawed, keep the best available
one rather than dropping the slide.

TASK 2 — PLACE. For each kept slide, choose the anchor it best illustrates (the point in the
summary whose topic the slide depicts; usually at or just before the slide's timestamp).

Return ONLY a JSON array, no prose, no code fences:
[{"slide": "MM:SS", "section": "MM:SS"}]
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


def _walkthrough_slice(summary_markdown: str) -> tuple[int, int]:
    """(start, end) character offsets of the Walkthrough section.

    Screenshots belong ONLY in the Walkthrough — not in TL;DR, Context, Key takeaways or
    Q&A, where an image interrupts the summary rather than illustrating a point.
    Falls back to the whole document if the section can't be located.
    """
    lines = summary_markdown.splitlines(keepends=True)
    start = end = None
    pos = 0
    for line in lines:
        if _HEADING_RE.match(line) or line.startswith("# "):
            is_walk = _WALKTHROUGH_RE.search(line) is not None
            if start is None and is_walk:
                start = pos
            elif start is not None and line.startswith(("## ", "# ")):
                # next top-level section closes the Walkthrough
                end = pos
                break
        pos += len(line)
    if start is None:
        return (0, len(summary_markdown))
    return (start, end if end is not None else len(summary_markdown))


def _section_anchors(summary_markdown: str) -> list[str]:
    """Timestamp anchors inside the Walkthrough, in order, de-duped.

    With the concept-organized Walkthrough, anchors live INLINE in prose rather than in
    section headings, so we collect them wherever they appear — but only within the
    Walkthrough, so slides can never be placed in TL;DR/Context/Takeaways/Q&A.
    """
    start, end = _walkthrough_slice(summary_markdown)
    seen: dict[str, None] = {}
    for m in _ANCHOR_RE.finditer(summary_markdown[start:end]):
        seen.setdefault(m.group(1), None)
    return list(seen)


def select_slide_placements(
    candidates: list[SlideCandidate],
    summary_markdown: str,
    config: Config,
    *,
    client: Any | None = None,
    batch_size: int = _SELECT_BATCH,
) -> list[dict]:
    """Vision call(s) → validated placements with per-slide cleanup regions.

    Candidates are processed in BATCHES. Sending every frame in one request made the model
    skip genuinely good slides — with 20+ images competing for attention it dropped a
    complete MHA/MQA/GQA comparison that it enthusiastically kept when shown on its own.
    Smaller batches give each frame real consideration; results are merged, and a final
    de-dupe keeps one frame per slide across batch boundaries.

    Returns [] on total failure or if the model keeps nothing. A failing batch is skipped
    rather than aborting the rest.
    """
    anchors = _section_anchors(summary_markdown)
    if not candidates or not anchors:
        return []
    client = client or make_client(config)

    entries: list[tuple[str, str, float]] = []
    for c in candidates:
        url = _data_url(c.path)
        if url is not None:
            entries.append((format_ts(c.timestamp), url, _caption_score(c.path)))
    if not entries:
        return []

    anchor_set = set(anchors)
    anchor_text = "Summary timestamp anchors (choose from these for 'section'):\n" + ", ".join(
        anchors
    )
    placements: list[dict] = []
    for start in range(0, len(entries), batch_size):
        chunk = entries[start : start + batch_size]
        # The caption score travels with the label so the model can prefer the cleaner of
        # two duplicates — it cannot judge subtitle occlusion reliably by eye alone.
        images = [
            (f"SLIDE {label} (caption {score:.4f})", url) for label, url, score in chunk
        ]
        text = (
            f"{anchor_text}\n\nThere are {len(chunk)} candidate slide frames below, in order"
            f" (batch {start // batch_size + 1} of"
            f" {(len(entries) + batch_size - 1) // batch_size})."
        )
        try:
            reply = call_responses_vision(client, config, _SELECT_INSTRUCTIONS, text, images)
        except Exception as exc:  # noqa: BLE001 - slides are additive; degrade
            log.warning("slide selection batch starting at %d failed: %s", start, exc)
            continue
        placements.extend(_parse_placements(reply, {lbl for lbl, _, _ in chunk}, anchor_set))

    placements = _dedupe_placements(placements)
    placements = _prefer_cleaner_duplicates(placements, candidates)
    log.info("slide selection: %d candidates -> %d kept", len(candidates), len(placements))
    return placements


def _edge_crop_score(image_path: Path) -> float:
    """How much slide content is running off the frame edges (0 = nothing cut off).

    When a recording is zoomed/panned, slide content is sliced by the frame border. A
    complete slide almost always has a quiet margin (background) at its edges, whereas a
    cropped one has ink right up against the border. We measure, for each of the four
    edges, how much non-background variation sits in the outermost band — high values mean
    content is being cut. Used to prefer the complete version of a duplicated slide.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - optional dependency
        return 0.0
    try:
        with Image.open(image_path) as src:
            gray = src.convert("L")
        gray = gray.resize((160, 120))
        w, h = gray.size
        px = list(gray.getdata())

        def band_activity(coords: list[int]) -> float:
            vals = [px[i] for i in coords]
            if len(vals) < 2:
                return 0.0
            mean = sum(vals) / len(vals)
            return sum(abs(v - mean) for v in vals) / len(vals) / 255.0

        band = 3
        left = [y * w + x for y in range(h) for x in range(band)]
        right = [y * w + x for y in range(h) for x in range(w - band, w)]
        top = [y * w + x for y in range(band) for x in range(w)]
        bottom = [y * w + x for y in range(h - band, h) for x in range(w)]
        return max(
            band_activity(left), band_activity(right),
            band_activity(top), band_activity(bottom),
        )
    except Exception:  # noqa: BLE001 - scoring is best-effort
        return 0.0


def _frame_penalty(path: Path) -> float:
    """Combined "how bad is this frame" score: caption occlusion + edge cropping.

    Both are things we cannot fix afterwards (frames are never cropped or masked), so the
    only remedy is to choose a different frame. Weighted so that a badly cropped frame is
    not accepted just because it happens to be caption-free.
    """
    return _caption_score(path) + _CROP_WEIGHT * _edge_crop_score(path)


def _prefer_cleaner_duplicates(
    placements: list[dict],
    candidates: list[SlideCandidate],
    *,
    window: float = _DUP_WINDOW_SECONDS,
) -> list[dict]:
    """Swap a kept frame for another shot of the SAME slide that shows it better.

    The model picks WHICH slide matters, but judges two mechanical properties poorly:
    subtitle occlusion and edge cropping. Both are measurable, so we correct them in code.
    Alternatives are candidates within `window` seconds of the kept frame. The window is
    generous because a talk lingers on one slide while zooming and re-captioning it, so the
    better-framed copy can be a minute away. We only switch when the penalty is CLEARLY
    lower, and the swap is logged. Timestamps stay honest: the placement's `slide` label is
    updated to the frame actually used.

    (A thumbnail-fingerprint check for "is this the same slide?" was tried and removed —
    zoom/pan differences swamped content differences, so same-slide pairs scored 26-31
    while different-slide pairs scored 30-40. No usable threshold.)
    """
    if not placements:
        return placements
    by_label = {format_ts(c.timestamp): c for c in candidates}
    scored = [(c, _frame_penalty(c.path)) for c in candidates]
    out: list[dict] = []
    for p in placements:
        current = by_label.get(p["slide"])
        if current is None:
            out.append(p)
            continue
        cur_score = _frame_penalty(current.path)
        best, best_score = current, cur_score
        for cand, score in scored:
            if abs(cand.timestamp - current.timestamp) > window:
                continue
            if score < best_score - _DUP_MARGIN:
                best, best_score = cand, score
        if best is not current:
            log.info(
                "slide %s: swapped to %s (penalty %.4f -> %.4f)",
                p["slide"], format_ts(best.timestamp), cur_score, best_score,
            )
            p = {**p, "slide": format_ts(best.timestamp)}
        out.append(p)
    return _dedupe_placements(out)


def _dedupe_placements(placements: list[dict]) -> list[dict]:
    """One entry per slide, ordered by timestamp (batches are independent, so a slide could
    in principle be reported twice)."""
    seen: dict[str, dict] = {}
    for p in placements:
        seen.setdefault(p["slide"], p)
    return sorted(seen.values(), key=lambda p: p["slide"])


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
        if slide not in valid_slides or section not in valid_sections or slide in seen:
            continue
        seen.add(slide)
        out.append({"slide": slide, "section": section})
    return out


def place_slides(summary_markdown: str, placements: list[dict]) -> str:
    """Insert `![slide@MM:SS]` placeholder lines next to the anchor each slide illustrates.

    Placement is CONFINED TO THE WALKTHROUGH: an image belongs where a point is being
    explained, not in TL;DR, Context, Key takeaways or Q&A. Anchors live inline in the
    prose, so a slide goes after the LINE that first mentions its target anchor. Prose is
    never modified — only placeholder lines are added. A slide whose anchor is missing (or
    lies outside the Walkthrough) is skipped.
    """
    if not placements:
        return summary_markdown
    by_anchor: dict[str, list[str]] = {}
    for p in placements:
        by_anchor.setdefault(p["section"], []).append(p["slide"])

    start, end = _walkthrough_slice(summary_markdown)
    out_lines: list[str] = []
    placed: set[str] = set()
    pos = 0
    for line in summary_markdown.splitlines(keepends=True):
        out_lines.append(line.rstrip("\n"))
        line_end = pos + len(line)
        inside = start <= pos and line_end <= end
        pos = line_end
        if not inside or _HEADING_RE.match(line):
            continue  # outside the Walkthrough, or between a heading and its first line
        for anchor in _ANCHOR_RE.findall(line):
            if anchor in by_anchor and anchor not in placed:
                placed.add(anchor)
                for slide_ts in by_anchor[anchor]:
                    out_lines.append("")
                    out_lines.append(f"![slide@{slide_ts}]")
    return "\n".join(out_lines)
