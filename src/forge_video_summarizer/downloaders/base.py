"""Downloader abstraction. Each site is one implementation behind this interface."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

from ..models import VideoMetadata

__all__ = ["DownloadResult", "Downloader"]


@dataclass
class DownloadResult:
    """What a downloader hands back: the video file path + rich metadata."""

    video_path: Path
    metadata: VideoMetadata


class Downloader(abc.ABC):
    """Interface every per-site downloader implements.

    Dependencies flow one way: the pipeline depends on this interface, never on
    a concrete site implementation.
    """

    @abc.abstractmethod
    def can_handle(self, url: str) -> bool:
        """True if this downloader recognizes the URL."""

    @abc.abstractmethod
    def fetch_metadata(self, url: str) -> VideoMetadata:
        """Fetch metadata without downloading the media."""

    @abc.abstractmethod
    def download(self, url: str, dest_dir: Path) -> DownloadResult:
        """Download the full video into dest_dir and return the result.

        Implementations MUST always download the full video (visual capability
        is planned) — never an audio-only shortcut.
        """
