"""Domain data structures shared across stages."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

__all__ = [
    "VideoMetadata",
    "TranscriptSegment",
    "Transcript",
    "format_timestamp",
]


def format_timestamp(seconds: float) -> str:
    """Seconds -> ``[MM:SS]`` or ``[HH:MM:SS]`` (the latter when >= 1 hour)."""
    if seconds < 0:
        seconds = 0
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


@dataclass
class VideoMetadata:
    """Provenance for a downloaded (or local) video. Serialized to metadata.json."""

    video_id: str
    title: str
    source_url: str = ""
    duration: float | None = None  # seconds
    uploader: str = ""
    uploader_id: str = ""
    upload_date: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    cover_image_url: str = ""
    view_count: int | None = None
    like_count: int | None = None
    coin_count: int | None = None
    favorite_count: int | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    resolution: str = ""
    download_timestamp: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VideoMetadata":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)


@dataclass
class TranscriptSegment:
    """One recognized phrase with timestamps referenced to the original timeline."""

    start: float  # seconds
    duration: float  # seconds
    text: str

    @property
    def end(self) -> float:
        return self.start + self.duration

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "duration": self.duration, "text": self.text}


@dataclass
class Transcript:
    """A full transcript: ordered segments + detected locale + full text."""

    segments: list[TranscriptSegment] = field(default_factory=list)
    locale: str = ""
    full_text: str = ""

    @property
    def duration(self) -> float:
        if not self.segments:
            return 0.0
        return max(seg.end for seg in self.segments)

    def to_timestamped_text(self) -> str:
        """Human-readable transcript with an inline anchor per segment."""
        lines = []
        for seg in self.segments:
            lines.append(f"{format_timestamp(seg.start)} {seg.text}".rstrip())
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            "full_text": self.full_text,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        segs = [
            TranscriptSegment(
                start=s["start"], duration=s["duration"], text=s["text"]
            )
            for s in data.get("segments", [])
        ]
        return cls(
            segments=segs,
            locale=data.get("locale", ""),
            full_text=data.get("full_text", ""),
        )
