"""forge-video-summarizer — video-to-summary pipeline.

Public API: the Pipeline plus the core config/model/error types.
"""

from __future__ import annotations

from .config import Config, load_config
from .errors import (
    AudioTooLongError,
    ConfigError,
    DownloadError,
    ExtractionError,
    ForgeError,
    SummarizationError,
    TranscriptionError,
    UnsupportedURLError,
)
from .models import Transcript, TranscriptSegment, VideoMetadata, format_timestamp
from .pipeline import Pipeline
from .workspace import Workspace, sanitize_title

__all__ = [
    "Pipeline",
    "Config",
    "load_config",
    "Workspace",
    "sanitize_title",
    "Transcript",
    "TranscriptSegment",
    "VideoMetadata",
    "format_timestamp",
    "ForgeError",
    "ConfigError",
    "DownloadError",
    "UnsupportedURLError",
    "ExtractionError",
    "TranscriptionError",
    "AudioTooLongError",
    "SummarizationError",
]

__version__ = "1.0.0"
