"""Bilibili downloader — distilled from the reference project's headless core.

Flow (auth = a single SESSDATA cookie):
  1. bvid -> cid + metadata   via  x/web-interface/view
  2. cid  -> DASH stream URLs via  x/player/wbi/playurl (fnval=4048, qn=127)
     DASH yields SEPARATE video-only and audio-only streams.
  3. Download video + audio streams (Referer header required or CDN 403s),
     mux with ffmpeg (-c copy, no re-encode) into a single mp4.

We always download the full video (not audio-only): visual capability is planned.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..errors import DownloadError
from ..models import VideoMetadata
from .base import DownloadResult, Downloader

__all__ = ["BilibiliDownloader", "normalize_bvid"]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_REFERER = "https://www.bilibili.com/"
_BVID_RE = re.compile(r"(BV[0-9A-Za-z]+)")
_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"


def normalize_bvid(text: str) -> str:
    """Extract a BV id from a raw id or a full bilibili URL."""
    m = _BVID_RE.search(text or "")
    if not m:
        raise DownloadError(f"Could not find a BV id in: {text!r}")
    return m.group(1)


class BilibiliDownloader(Downloader):
    def __init__(self, sessdata: str = "", *, session: requests.Session | None = None):
        self._sessdata = sessdata
        self._session = session or self._make_session(sessdata)

    @staticmethod
    def _make_session(sessdata: str) -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA, "Referer": _REFERER})
        if sessdata:
            s.cookies.set("SESSDATA", sessdata.strip().rstrip(";"))
        return s

    # ── interface ───────────────────────────────────────────────────────
    def can_handle(self, url: str) -> bool:
        return "bilibili.com" in (url or "") or bool(_BVID_RE.search(url or ""))

    def fetch_metadata(self, url: str) -> VideoMetadata:
        bvid = normalize_bvid(url)
        data = self._get_view(bvid)
        return self._build_metadata(url, bvid, data)

    def download(self, url: str, dest_dir: Path) -> DownloadResult:
        bvid = normalize_bvid(url)
        data = self._get_view(bvid)
        metadata = self._build_metadata(url, bvid, data)

        pages = data.get("pages") or []
        if not pages:
            raise DownloadError(f"No parts found for {bvid}")
        cid = pages[0]["cid"]  # v1: first part

        video_url, audio_url = self._get_play_urls(bvid, cid)

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        v_tmp = dest / "_video.m4s"
        a_tmp = dest / "_audio.m4s"
        self._download_stream(video_url, v_tmp)
        self._download_stream(audio_url, a_tmp)

        out = dest / "video.mp4"
        self._merge(v_tmp, a_tmp, out)
        v_tmp.unlink(missing_ok=True)
        a_tmp.unlink(missing_ok=True)

        return DownloadResult(video_path=out, metadata=metadata)

    # ── bilibili API calls ──────────────────────────────────────────────
    def _get_view(self, bvid: str) -> dict:
        try:
            r = self._session.get(_VIEW_API, params={"bvid": bvid}, timeout=10)
            payload = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise DownloadError(f"view API request failed: {exc}") from exc
        if payload.get("code") != 0:
            raise DownloadError(f"view API error: {payload.get('message')}")
        return payload["data"]

    def _get_play_urls(self, bvid: str, cid: int) -> tuple[str, str]:
        """cid -> (video_url, audio_url). WBI signing is not required for playurl."""
        try:
            r = self._session.get(
                _PLAYURL_API,
                params={"bvid": bvid, "cid": cid, "qn": 127, "fnval": 4048},
                timeout=10,
            )
            payload = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise DownloadError(f"playurl API request failed: {exc}") from exc
        if payload.get("code") != 0:
            raise DownloadError(f"playurl API error: {payload.get('message')}")
        dash = (payload.get("data") or {}).get("dash")
        if not dash or not dash.get("video") or not dash.get("audio"):
            raise DownloadError("playurl returned no DASH streams")
        return dash["video"][0]["baseUrl"], dash["audio"][0]["baseUrl"]

    def _download_stream(self, url: str, out_path: Path) -> None:
        try:
            with self._session.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
        except requests.RequestException as exc:
            raise DownloadError(f"stream download failed: {exc}") from exc

    @staticmethod
    def _merge(video_path: Path, audio_path: Path, out_path: Path) -> None:
        try:
            subprocess.run(
                [
                    "ffmpeg", "-i", str(video_path), "-i", str(audio_path),
                    "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
                    str(out_path), "-y",
                ],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise DownloadError(f"ffmpeg merge failed: {exc}") from exc

    # ── metadata mapping ────────────────────────────────────────────────
    @staticmethod
    def _build_metadata(url: str, bvid: str, data: dict) -> VideoMetadata:
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        return VideoMetadata(
            video_id=bvid,
            title=data.get("title", bvid),
            source_url=url,
            duration=data.get("duration"),
            uploader=owner.get("name", ""),
            uploader_id=str(owner.get("mid", "")),
            upload_date=str(data.get("pubdate", "")),
            description=data.get("desc", ""),
            tags=[data["tname"]] if data.get("tname") else [],
            cover_image_url=data.get("pic", ""),
            view_count=stat.get("view"),
            like_count=stat.get("like"),
            coin_count=stat.get("coin"),
            favorite_count=stat.get("favorite"),
            parts=data.get("pages") or [],
            download_timestamp=datetime.now(timezone.utc).isoformat(),
        )
