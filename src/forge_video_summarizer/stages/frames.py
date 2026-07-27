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
  5. Frames are used EXACTLY AS CAPTURED — never cropped, never masked. Cropping and
     masking were tried and removed: they destroyed real slide content (a chrome-crop
     heuristic cut 25% off a light-background slide) and a mis-placed mask silently hides
     information. An unwanted webcam in a corner is cosmetic; missing slide content is not.
     The only lever we use against on-screen clutter is WHICH frame we pick (step 4).

The model (Stage 4b `slides.py`) then decides which candidates are actually key slides and
where each belongs. This stage only PRODUCES candidates; it makes no keep/drop judgment.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "SlideCandidate",
    "detect_slide_candidates",
    "format_ts",
    "SCENE_THRESHOLD",
]

# Scene score cut (0-1). Deliberately permissive: soft/animated slide changes score low,
# and a missed slide can never be recovered downstream, while extra candidates are cheap
# (the vision model prunes them).
SCENE_THRESHOLD = 0.27
_MIN_GAP_SECONDS = 8.0  # candidates closer than this collapse into one slide cluster
_MAX_GAP_SECONDS = 45.0  # longer stretches get filled with extra samples (recall backstop)
_CAPTION_SAMPLES = 16  # frames sampled per slide window when hunting a caption-free one
_CAPTION_STEP = 1.0  # seconds between those samples
_CAPTION_CLEAN = 0.004  # score at/below this counts as "no caption" — stop searching
_SHOWINFO_TS = re.compile(r"pts_time:([0-9.]+)")



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
    """How much of the frame is covered by a subtitle caption, weighted by what it hides.

    Two signals, because caption PRESENCE alone is the wrong target — what matters is
    whether the caption OCCLUDES slide content:

      1. coverage — fraction of bright-yellow caption pixels in the bottom strip.
      2. occlusion — whether the caption band sits over busy slide content (text, boxes,
         diagrams) rather than empty margin. A caption floating over blank space at the
         bottom of a slide is harmless; one sitting on top of a label or formula is not.

    Returns 0.0 for a caption-free frame; larger is worse. If Pillow is unavailable,
    returns 0.0 so the pipeline still works (just no caption avoidance).
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - optional dependency
        return 0.0
    try:
        with Image.open(image_path) as src:
            im = src.convert("RGB")
        w, h = im.size
        strip = im.crop((0, int(h * 0.72), w, h))
        small = strip.resize((max(1, strip.width // 4), max(1, strip.height // 4)))
        px = list(small.getdata())
        if not px:
            return 0.0
        yellow = [
            i for i, (r, g, b) in enumerate(px) if r > 150 and g > 150 and b < 120
        ]
        coverage = len(yellow) / len(px)
        if coverage == 0.0:
            return 0.0
        # Occlusion: how busy are the rows the caption covers? Busy => it is hiding
        # something. Measured on the greyscale strip as mean horizontal contrast.
        gray = small.convert("L")
        gw, gh = gray.size
        rows = {yellow_i // gw for yellow_i in yellow}
        data = list(gray.getdata())
        busy = 0.0
        for row in rows:
            line = data[row * gw : (row + 1) * gw]
            if len(line) > 1:
                busy += sum(
                    abs(line[i + 1] - line[i]) for i in range(len(line) - 1)
                ) / (len(line) - 1)
        busy = busy / max(1, len(rows)) / 255.0
        return coverage * (1.0 + 2.0 * busy)
    except Exception:  # noqa: BLE001 - scoring is best-effort
        return 0.0


def _pick_clean_frame(video: Path, t: float, out_dir: Path) -> Path | None:
    """Pick the frame near time t whose caption hides the least.

    Captions are transient, so we sample forward across the slide's window and keep the
    lowest-scoring frame, stopping early once a genuinely caption-free frame turns up.
    This is the ONLY mechanism we use against on-screen captions — frames are never
    cropped or masked, so a caption is avoided by choosing a different moment or not at all.
    """
    best_path: Path | None = None
    best_score = float("inf")
    for i in range(_CAPTION_SAMPLES):
        sample_t = t + i * _CAPTION_STEP
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
        if best_score <= _CAPTION_CLEAN:
            break
    if best_path is None:
        return None
    if best_score > _CAPTION_CLEAN:
        log.debug("no caption-free frame near %.1fs (best score %.4f)", t, best_score)
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

    Frames are returned exactly as captured — no cropping, no masking.

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
