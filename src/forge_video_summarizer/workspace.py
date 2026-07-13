"""Per-video working directory and filename sanitization.

A Workspace owns the paths for one video's artifacts and encapsulates the
cache-or-skip rule shared by every stage.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["sanitize_title", "Workspace"]

# Characters illegal on common filesystems (Windows is the strictest).
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_MAX_NAME_LEN = 150


def sanitize_title(title: str) -> str:
    """Make a filesystem-safe directory stem from a raw video title.

    Native handling only (no LLM): replace invalid chars, collapse whitespace,
    strip trailing dots/spaces, cap length. Never returns an empty string.
    """
    cleaned = _INVALID_CHARS.sub("_", title)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if len(cleaned) > _MAX_NAME_LEN:
        cleaned = cleaned[:_MAX_NAME_LEN].rstrip(". ")
    return cleaned or "untitled"


class Workspace:
    """Filesystem layout for a single video's pipeline run.

    Directory name = ``<sanitized-title>[<video-id>]`` — human-readable with a
    short id suffix as a collision guard.
    """

    def __init__(self, root: Path, title: str, video_id: str):
        self.root = Path(root)
        self.title = title
        self.video_id = video_id
        stem = sanitize_title(title)
        suffix = f"[{video_id}]" if video_id else ""
        self.dir = self.root / f"{stem}{suffix}"

    # ── artifact paths ──────────────────────────────────────────────────
    @property
    def metadata_path(self) -> Path:
        return self.dir / "metadata.json"

    @property
    def audio_path(self) -> Path:
        return self.dir / "audio.wav"

    @property
    def transcript_json_path(self) -> Path:
        return self.dir / "transcript.json"

    @property
    def transcript_txt_path(self) -> Path:
        return self.dir / "transcript.txt"

    @property
    def summary_path(self) -> Path:
        return self.dir / "summary.md"

    @property
    def overview_image_path(self) -> Path:
        return self.dir / "overview.png"

    @property
    def notion_url_path(self) -> Path:
        return self.dir / "notion_url.txt"

    def video_path(self, ext: str = "mp4") -> Path:
        return self.dir / f"video.{ext.lstrip('.')}"

    def find_video(self) -> Path | None:
        """Return the first downloaded video file present, if any."""
        if not self.dir.is_dir():
            return None
        for candidate in sorted(self.dir.glob("video.*")):
            if candidate.is_file():
                return candidate
        return None

    # ── helpers ─────────────────────────────────────────────────────────
    def ensure(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    @staticmethod
    def should_skip(path: Path, force: bool) -> bool:
        """Cache rule: skip when the artifact already exists and not forcing."""
        return path.exists() and not force
