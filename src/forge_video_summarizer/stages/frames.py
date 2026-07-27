"""Slide-frame extraction (optional `--slides` capability).

For slide/PPT-style talks, real slide screenshots beat a synthesized diagram. This
stage finds the distinct slides shown in the video and picks a clean frame for each:

  1. Scene-change detection (`ffmpeg select='gt(scene,THR)'`) -> candidate timestamps.
     Slide transitions produce a scene cut; so do mid-slide animations.
  2. Dedup: cluster candidates that are close in time (an animated slide re-triggers
     several cuts); keep ONE representative per cluster.
  3. Gap backstop: soft/animated slide transitions can score BELOW the threshold, leaving
     long stretches with no candidate at all (a real slide can hide there). Any gap longer
     than `_MAX_GAP_SECONDS` is filled with evenly-spaced extra samples. Recall first —
     the vision model is the quality gate and prunes what isn't useful.
  4. Caption-aware pick: within each slide's window, sample several frames and choose the
     one with the LEAST on-screen subtitle (bright caption bar in the bottom strip).
  5. Cleanup (`apply_cleanup`): remove overlays that aren't slide content — the presenter's
     webcam bubble (which can sit in ANY corner and moves between videos) and browser/app
     chrome (tabs, URL bar). These regions are reported by the VISION MODEL in
     `slides.py` — it already looks at every candidate, so it identifies them far more
     reliably than pixel heuristics, which break across dark/light slide backgrounds.
     The webcam patch is filled with the ring-median colour so it blends into the slide.

The model (Stage 4b `slides.py`) then decides which candidates are actually key slides and
where each belongs. This stage only PRODUCES candidates; it makes no keep/drop judgment.
"""

from __future__ import annotations

import logging
import re
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "SlideCandidate",
    "detect_slide_candidates",
    "apply_cleanup",
    "format_ts",
    "SCENE_THRESHOLD",
]

# Scene score cut (0-1). Deliberately permissive: soft/animated slide changes score low,
# and a missed slide can never be recovered downstream, while extra candidates are cheap
# (the vision model prunes them).
SCENE_THRESHOLD = 0.27
_MIN_GAP_SECONDS = 8.0  # candidates closer than this collapse into one slide cluster
_MAX_GAP_SECONDS = 45.0  # longer stretches get filled with extra samples (recall backstop)
_CAPTION_SAMPLES = 5  # frames sampled per slide window to find a caption-free one
_SHOWINFO_TS = re.compile(r"pts_time:([0-9.]+)")

# Overlay-cleanup tuning (regions come from the vision model; these are sanity bounds)
_MASK_PAD = 0.03  # pad the detected webcam box by this fraction of the frame
_MAX_MASK_AREA = 0.20  # refuse to mask more than this share of the frame (safety)
_CHROME_MAX = 0.25  # only look for UI chrome within the top fraction of the frame


@dataclass
class SlideCandidate:
    timestamp: float  # seconds into the video
    path: Path  # the extracted PNG


def format_ts(seconds: float) -> str:
    """Seconds -> MM:SS or HH:MM:SS (matches the summary's [MM:SS] anchor grammar)."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError("ffmpeg is required for slide extraction but was not found")


def _probe_duration(video: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None


def _scene_timestamps(video: Path, threshold: float) -> list[float]:
    """All scene-change timestamps (seconds) via ffmpeg select+showinfo."""
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(video),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # showinfo writes to stderr; parse pts_time of each selected frame.
    return sorted(float(m) for m in _SHOWINFO_TS.findall(proc.stderr))


def _cluster(timestamps: list[float], min_gap: float) -> list[float]:
    """Collapse near-consecutive cuts (mid-slide animations) into one time per slide.

    Keep the LAST timestamp of each cluster — by then the slide is fully drawn.
    """
    if not timestamps:
        return []
    clusters: list[list[float]] = [[timestamps[0]]]
    for t in timestamps[1:]:
        if t - clusters[-1][-1] <= min_gap:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [c[-1] for c in clusters]


def _fill_gaps(times: list[float], duration: float | None, max_gap: float) -> list[float]:
    """Insert evenly-spaced samples wherever consecutive slide times are too far apart.

    Soft transitions can score below the scene threshold, so a real slide may live inside a
    long quiet stretch with no candidate. This guarantees such a stretch is still sampled.
    """
    bounds = [0.0, *times]
    if duration:
        bounds.append(duration)
    filled: list[float] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        filled.append(a)
        span = b - a
        if span > max_gap:
            extra = int(span // max_gap)
            step = span / (extra + 1)
            filled.extend(a + step * (i + 1) for i in range(extra))
    filled.append(bounds[-1])
    # de-dup / sort, drop anything at/after the very end
    out = sorted({round(t, 2) for t in filled if t >= 0})
    if duration:
        out = [t for t in out if t < duration - 1]
    return out


def _extract_frame(video: Path, t: float, out_path: Path) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out_path), "-y",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError):
        return False
    return out_path.is_file() and out_path.stat().st_size > 0


def _caption_score(image_path: Path) -> float:
    """Fraction of bright-yellow pixels in the bottom strip (subtitle caption proxy).

    ~0 for a caption-free frame; higher when a subtitle bar is present. If Pillow is
    unavailable, return 0.0 so the pipeline still works (just no caption avoidance).
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - optional dependency
        return 0.0
    try:
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            strip = im.crop((0, int(h * 0.80), w, h))
            strip = strip.resize((max(1, strip.width // 4), max(1, strip.height // 4)))
            px = list(strip.getdata())
            if not px:
                return 0.0
            yellow = sum(1 for r, g, b in px if r > 150 and g > 150 and b < 120)
            return yellow / len(px)
    except Exception:  # noqa: BLE001 - scoring is best-effort
        return 0.0


def apply_cleanup(
    image_path: Path,
    *,
    chrome_bottom: float = 0.0,
    webcam: tuple[float, float, float, float] | None = None,
) -> bool:
    """Remove non-slide overlays from a frame, using regions reported by the vision model.

    `chrome_bottom` — fraction of height where top browser/app chrome ends (0 = none);
    that strip is cropped off. `webcam` — (x0, x1, y0, y1) fractions of the presenter's
    camera bubble, in ANY corner; it is filled with the median colour of a ring just
    outside the box so the patch blends into the slide background.

    Edits the PNG in place. Returns True if anything changed. Best-effort: bad/implausible
    regions are ignored and the original frame is left intact.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - optional dependency
        return False
    try:
        with Image.open(image_path) as src:
            im = src.convert("RGB")
        w, h = im.size
        changed = False

        if webcam:
            x0, x1, y0, y1 = webcam
            if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1 \
                    and (x1 - x0) * (y1 - y0) <= _MAX_MASK_AREA:
                bx0 = max(0, int(w * (x0 - _MASK_PAD)))
                bx1 = min(w, int(w * (x1 + _MASK_PAD)))
                by0 = max(0, int(h * (y0 - _MASK_PAD)))
                by1 = min(h, int(h * (y1 + _MASK_PAD)))
                if bx1 > bx0 and by1 > by0:
                    fill = _ring_median(im, bx0, bx1, by0, by1)
                    ImageDraw.Draw(im).rectangle((bx0, by0, bx1, by1), fill=fill)
                    changed = True

        if 0 < chrome_bottom < _CHROME_MAX:
            cut = int(h * chrome_bottom)
            if 0 < cut < h - 10:
                im = im.crop((0, cut, w, h))
                changed = True

        if changed:
            im.save(image_path)
        return changed
    except Exception:  # noqa: BLE001 - cleanup is additive; never break the pipeline
        return False


def _ring_median(im, bx0: int, bx1: int, by0: int, by1: int) -> tuple[int, int, int]:
    """Median colour of a ring just outside the box — a robust background estimate that
    blends the patch on both dark and light slides."""
    w, h = im.size
    ring = 12
    samples = []
    for yy in range(by0, by1, 6):
        for xx in (max(0, bx0 - ring), min(w - 1, bx1 + ring)):
            samples.append(im.getpixel((xx, yy)))
    for xx in range(bx0, bx1, 6):
        for yy in (max(0, by0 - ring), min(h - 1, by1 + ring)):
            samples.append(im.getpixel((xx, yy)))
    if not samples:
        return (0, 0, 0)
    return tuple(  # type: ignore[return-value]
        int(statistics.median(c[i] for c in samples)) for i in range(3)
    )


def _pick_clean_frame(video: Path, t: float, out_dir: Path) -> Path | None:
    """Pick the least-captioned frame near time t. Samples a few frames from t onward
    (captions are transient — later frames on the same slide are often caption-free)."""
    best_path: Path | None = None
    best_score = float("inf")
    for i in range(_CAPTION_SAMPLES):
        sample_t = t + i * 1.5
        tmp = out_dir / f"_probe_{i}.png"
        if not _extract_frame(video, sample_t, tmp):
            continue
        score = _caption_score(tmp)
        if score < best_score:
            best_score = score
            if best_path is not None and best_path != tmp:
                best_path.unlink(missing_ok=True)
            best_path = tmp
        else:
            tmp.unlink(missing_ok=True)
        if best_score <= 0.001:
            break
    if best_path is None:
        return None
    final = out_dir / f"slide_{format_ts(t).replace(':', '-')}.png"
    best_path.replace(final)
    return final


def detect_slide_candidates(
    video: str | Path,
    out_dir: str | Path,
    *,
    threshold: float = SCENE_THRESHOLD,
) -> list[SlideCandidate]:
    """Detect distinct slides and extract one caption-free frame each.

    Overlay cleanup (webcam mask / chrome crop) happens later, in `slides.py`, using
    regions the vision model reports — see `apply_cleanup`.

    Returns candidates ordered by timestamp. Never raises on a per-frame failure —
    only if ffmpeg itself is missing.
    """
    _require_ffmpeg()
    video = Path(video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration(video)
    scene_ts = _scene_timestamps(video, threshold)
    slide_ts = _cluster(scene_ts, _MIN_GAP_SECONDS)
    slide_ts = _fill_gaps(slide_ts, duration, _MAX_GAP_SECONDS)

    candidates: list[SlideCandidate] = []
    for t in slide_ts:
        frame = _pick_clean_frame(video, t, out_dir)
        if frame is not None:
            candidates.append(SlideCandidate(timestamp=t, path=frame))
    log.info(
        "slide detection: %d scene cuts -> %d sampled -> %d frames",
        len(scene_ts), len(slide_ts), len(candidates),
    )
    return candidates
