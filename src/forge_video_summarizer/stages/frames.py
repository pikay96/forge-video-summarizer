"""Slide-frame extraction (optional `--slides` capability).

For slide/PPT-style talks, real slide screenshots beat a synthesized diagram. This
stage finds the distinct slides shown in the video and picks a clean frame for each:

  1. Scene-change detection (`ffmpeg select='gt(scene,THR)'`) -> candidate timestamps.
     Slide transitions produce a scene cut; so do mid-slide animations.
  2. Dedup: cluster candidates that are close in time (an animated slide re-triggers
     several cuts); keep ONE representative per cluster.
  3. Caption-aware pick: within each cluster's window, sample several frames and choose
     the one with the LEAST on-screen subtitle (a bright caption bar in the bottom strip),
     falling back to the cluster frame. Full frame, original aspect ratio, no crop — the
     webcam corner is intentionally left in.

The model (Stage 4b `slides.py`) then decides which of these candidates are actually key
slides and which walkthrough section each belongs to. This stage only PRODUCES candidates;
it makes no keep/drop judgment itself.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["SlideCandidate", "detect_slide_candidates", "format_ts", "SCENE_THRESHOLD"]

SCENE_THRESHOLD = 0.4  # ffmpeg scene score cut (0-1); higher = fewer, cleaner cuts
_MIN_GAP_SECONDS = 8.0  # candidates closer than this collapse into one slide cluster
_CAPTION_SAMPLES = 5  # frames sampled per cluster window to find a caption-free one
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


def _scene_timestamps(video: Path, threshold: float) -> list[float]:
    """All scene-change timestamps (seconds) via ffmpeg select+showinfo."""
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(video),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # showinfo writes to stderr; parse pts_time of each selected frame.
    times = [float(m) for m in _SHOWINFO_TS.findall(proc.stderr)]
    return sorted(times)


def _cluster(timestamps: list[float], min_gap: float) -> list[float]:
    """Collapse near-consecutive cuts (mid-slide animations) into one time per slide.

    Keep the LAST timestamp of each cluster — by then the slide is fully drawn.
    Always include t=0 as the opening slide if the first cut is well after the start.
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
            # downsample for speed; count bright-yellow (hi R, hi G, lo B) pixels
            strip = strip.resize((max(1, strip.width // 4), max(1, strip.height // 4)))
            px = list(strip.getdata())
            if not px:
                return 0.0
            yellow = sum(1 for r, g, b in px if r > 150 and g > 150 and b < 120)
            return yellow / len(px)
    except Exception:  # noqa: BLE001 - scoring is best-effort
        return 0.0


def _pick_clean_frame(video: Path, t: float, out_dir: Path) -> Path | None:
    """Pick the least-captioned frame near time t. Samples a few frames from t onward
    (captions are transient — later frames on the same slide are often caption-free)."""
    best_path: Path | None = None
    best_score = float("inf")
    for i in range(_CAPTION_SAMPLES):
        sample_t = t + i * 1.5  # step forward 1.5s each sample
        tmp = out_dir / f"_probe_{int(round(sample_t))}.png"
        if not _extract_frame(video, sample_t, tmp):
            continue
        score = _caption_score(tmp)
        if score < best_score:
            best_score = score
            if best_path and best_path != tmp:
                best_path.unlink(missing_ok=True)
            best_path = tmp
        else:
            tmp.unlink(missing_ok=True)
        if best_score <= 0.001:  # already clean, stop early
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
    """Detect distinct slides and extract one clean (caption-free) frame per slide.

    Returns candidates ordered by timestamp. Never raises on a per-frame failure —
    only if ffmpeg itself is missing.
    """
    _require_ffmpeg()
    video = Path(video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scene_ts = _scene_timestamps(video, threshold)
    slide_ts = _cluster(scene_ts, _MIN_GAP_SECONDS)
    # Ensure the opening slide is represented even if the first cut comes late.
    if not slide_ts or slide_ts[0] > _MIN_GAP_SECONDS:
        slide_ts = [0.0, *slide_ts]

    candidates: list[SlideCandidate] = []
    for t in slide_ts:
        frame = _pick_clean_frame(video, t, out_dir)
        if frame is not None:
            candidates.append(SlideCandidate(timestamp=t, path=frame))
    log.info("slide detection: %d scene cuts -> %d slides", len(scene_ts), len(candidates))
    return candidates
