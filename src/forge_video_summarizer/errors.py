"""Typed exceptions for the pipeline. One place so callers can catch precisely."""

from __future__ import annotations

__all__ = [
    "ForgeError",
    "ConfigError",
    "DownloadError",
    "UnsupportedURLError",
    "ExtractionError",
    "TranscriptionError",
    "AudioTooLongError",
    "SummarizationError",
    "ExportError",
]


class ForgeError(Exception):
    """Base for all pipeline errors."""


class ConfigError(ForgeError):
    """Missing or invalid configuration (e.g. absent .env credentials)."""


class DownloadError(ForgeError):
    """A source download failed (network, API error, missing stream)."""


class UnsupportedURLError(DownloadError):
    """No registered downloader can handle the given URL."""


class ExtractionError(ForgeError):
    """ffmpeg audio extraction failed."""


class TranscriptionError(ForgeError):
    """Azure Speech transcription failed."""


class AudioTooLongError(TranscriptionError):
    """Audio exceeds the fast-transcription limits (v1 does not chunk)."""


class SummarizationError(ForgeError):
    """Azure OpenAI summarization failed."""


class ExportError(ForgeError):
    """Notion export failed (auth, unreachable page, API error)."""
