"""Downloader registry: pick the right per-site downloader for a URL."""

from __future__ import annotations

from ..config import Config
from ..errors import UnsupportedURLError
from .base import Downloader, DownloadResult
from .bilibili import BilibiliDownloader, normalize_bvid

__all__ = [
    "DownloadResult",
    "Downloader",
    "BilibiliDownloader",
    "normalize_bvid",
    "get_downloader",
    "build_downloaders",
]


def build_downloaders(config: Config) -> list[Downloader]:
    """Construct all registered downloaders. Add new sites here."""
    return [BilibiliDownloader(sessdata=config.bili_sessdata)]


def get_downloader(url: str, config: Config) -> Downloader:
    """Return the first downloader that can handle the URL, or raise."""
    for dl in build_downloaders(config):
        if dl.can_handle(url):
            return dl
    raise UnsupportedURLError(f"No downloader can handle URL: {url!r}")
