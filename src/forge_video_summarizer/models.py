"""Domain data structures shared across stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

__all__ = ["VideoMetadata", "TranscriptSegment", "Transcript", "format_timestamp"]


def format_timestamp(seconds: float) -> str:
    """Seconds -> ``[MM:SS]`` or ``[HH:MM:SS]`` (the latter when >= 1 hour)."""
    h, rem = divmod(max(0, int(round(seconds))), 3600)
    m, s = divmod(rem, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"


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
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class TranscriptSegment:
    """One recognized phrase with timestamps on the original timeline."""

    start: float  # seconds
    duration: float  # seconds
    text: str

    @property
    def end(self) -> float:
        return self.start + self.duration

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Transcript:
    """A full transcript: ordered segments + detected locale + full text.

    ``segments`` timestamps may be INTERPOLATED (see the transcribe stage) when the
    ASR model returns a single phrase. ``raw`` holds the untouched API response as the
    real source of truth; it is not part of ``to_dict`` serialization.
    """

    segments: list[TranscriptSegment] = field(default_factory=list)
    locale: str = ""
    full_text: str = ""
    approximate_timestamps: bool = False
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    def to_timestamped_text(self) -> str:
        """Human-readable transcript with an inline anchor per segment.

        Prepends a header disclosing when anchors are approximate/interpolated.
        """
        body = "\n".join(
            f"{format_timestamp(s.start)} {s.text}".rstrip() for s in self.segments
        )
        if self.approximate_timestamps:
            header = (
                "# NOTE: timestamps are APPROXIMATE — the transcription model returned a\n"
                "# single block, so per-line [MM:SS] anchors are interpolated by sentence\n"
                "# position across the total duration (only 0:00 and the end are exact).\n\n"
            )
            return header + body
        return body

    def to_dict(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            "full_text": self.full_text,
            "approximate_timestamps": self.approximate_timestamps,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        return cls(
            segments=[TranscriptSegment(**s) for s in data.get("segments", [])],
            locale=data.get("locale", ""),
            full_text=data.get("full_text", ""),
            approximate_timestamps=data.get("approximate_timestamps", False),
        )
