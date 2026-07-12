"""Stage 2 — Audio extraction. ffmpeg -> 16 kHz mono PCM WAV.

Plain extraction (no loudness normalization / silence trimming) so the audio
timeline stays identical to the source and anchors remain accurate. Output is WAV
(16 kHz mono PCM) — a format the fast-transcription endpoint accepts directly, and
what a future SDK path would want too. WAV is larger than MP3 on disk (~115 MB/h).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..errors import ExtractionError

__all__ = ["extract_audio", "probe_duration"]


def extract_audio(video_path: str | Path, out_path: str | Path, *, force: bool = False) -> Path:
    """Extract 16 kHz mono PCM WAV from a video file. Cache-skips unless force."""
    video_path = Path(video_path)
    out_path = Path(out_path)

    if not video_path.is_file():
        raise ExtractionError(f"Video file not found: {video_path}")
    if out_path.exists() and not force:
        return out_path  # cache hit

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_path), "-y",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ExtractionError(f"ffmpeg extraction failed: {exc}") from exc

    if not out_path.is_file():
        raise ExtractionError("ffmpeg reported success but no output file was produced")
    return out_path


def probe_duration(media_path: str | Path) -> float | None:
    """Return media duration in seconds via ffprobe, or None if unavailable."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    raw = result.stdout.strip()
    try:
        return float(raw)
    except ValueError:
        return None
