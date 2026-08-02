"""Douyin (抖音) downloader.

Thin wrapper over Douyin's web detail API, following the same shape as the other
site downloaders: resolve the URL to an id, fetch metadata, pick a video stream,
stream it to disk.

Three things make Douyin harder than the other sites, all verified live:

1. **The page HTML carries no video data.** Unlike Xiaohongshu (which embeds
   `window.__INITIAL_STATE__`), fetching the video page returns markup with no
   `play_addr` anywhere, so the JSON API is the only route.
2. **Unsigned API calls fail silently.** `/aweme/v1/web/aweme/detail/` answers
   HTTP 200 with a ZERO-BYTE body when the `a_bogus` signature is missing —
   there is no error message to react to. Signing is done by the vendored
   `_abogus` module.
3. **A signature alone is still not enough.** The same request returns an empty
   body without an `msToken` + `ttwid` cookie pair. Both are obtainable
   programmatically (see `_bootstrap_credentials`), so no user cookie is needed
   for public videos — matching the zero-config behaviour of the other sites.

`DOUYIN_COOKIE` remains available for the case where anti-bot tightens up.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from ..errors import DownloadError
from ..models import VideoMetadata
from ._abogus import ABogus, BrowserFingerprintGenerator
from .base import Downloader, DownloadResult

__all__ = ["DouyinDownloader", "extract_aweme_id"]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_REFERER = "https://www.douyin.com/?recommend=1"
_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_TTWID_API = "https://ttwid.bytedance.com/ttwid/union/register/"

_HOST_RE = re.compile(r"(?:douyin\.com|iesdouyin\.com)", re.I)
# Share shortener handed out by the Douyin app: https://v.douyin.com/<code>/
_SHORT_RE = re.compile(r"https?://v\.douyin\.com/\S+", re.I)
# /video/<id>, /note/<id>, and the ?modal_id=<id> form used by feed permalinks.
_ID_RE = re.compile(r"/(?:video|note|slides)/(\d+)")
_MODAL_ID_RE = re.compile(r"[?&]modal_id=(\d+)")

# Query parameters the web player sends. Douyin validates these loosely, but the
# signature is computed over the whole query string, so they must stay stable.
_BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "136.0.0.0",
    "engine_name": "Blink",
    "os_name": "Windows",
    "os_version": "10",
    "platform": "PC",
}

# Stream preference: h264 plays everywhere and is what ffmpeg handles most
# predictably downstream; the h265/bytevc1 variants are smaller but decode
# inconsistently. `play_addr` is the default the web player itself uses.
_STREAM_KEYS = ("play_addr_h264", "play_addr", "download_addr", "play_addr_265")


def extract_aweme_id(url: str) -> str:
    """Extract the numeric aweme id from any Douyin video URL form."""
    for pattern in (_ID_RE, _MODAL_ID_RE):
        m = pattern.search(url or "")
        if m:
            return m.group(1)
    raise DownloadError(f"Could not find a Douyin video id in: {url!r}")


def _gen_ms_token() -> str:
    """A random msToken of the length Douyin's web client uses.

    The real token comes from an obfuscated SDK endpoint; a random 182+`==`
    string of the same shape is accepted for public video reads and keeps this
    downloader dependency-free. Verified live against the detail API.
    """
    body = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(182))
    return f"{body}=="


class DouyinDownloader(Downloader):
    def __init__(self, cookie: str = "", *, session: requests.Session | None = None):
        self._cookie = (cookie or "").strip()
        self._session = session or self._make_session()
        self._bootstrapped = False

    @staticmethod
    def _make_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": _UA,
            "Referer": _REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        return s

    # ── interface ───────────────────────────────────────────────────────
    def can_handle(self, url: str) -> bool:
        url = url or ""
        return bool(_HOST_RE.search(url)) or bool(_SHORT_RE.match(url))

    def fetch_metadata(self, url: str) -> VideoMetadata:
        detail = self._get_detail(url)
        return self._build_metadata(url, detail)

    def download(self, url: str, dest_dir: Path) -> DownloadResult:
        detail = self._get_detail(url)
        metadata = self._build_metadata(url, detail)
        stream_url = self._video_url(detail)
        if not stream_url:
            raise DownloadError(
                "No downloadable video stream found — this is likely an image/gallery "
                f"post rather than a video: {url!r}"
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / "video.mp4"
        self._download_stream(stream_url, out_path)
        return DownloadResult(video_path=out_path, metadata=metadata)

    # ── internals ───────────────────────────────────────────────────────
    def _resolve(self, url: str) -> str:
        """Follow a v.douyin.com share link to the canonical /video/<id> URL.

        Done on a THROWAWAY session, like the ttwid registration. The redirect
        chain sets `__ac_nonce` and a *challenge* `ttwid` on whatever session
        follows it, which marks that session as pending anti-bot verification —
        every later signed API call then returns HTTP 200 with an empty body.
        Worse, the planted `ttwid` made `_bootstrap_credentials` believe it
        already had a good cookie and skip fetching a real one, so a share link
        failed while the equivalent direct URL succeeded.
        """
        if not _SHORT_RE.match(url or ""):
            return url
        throwaway = requests.Session()
        throwaway.headers.update({"User-Agent": _UA})
        try:
            resp = throwaway.get(url, allow_redirects=True, timeout=20)
            return resp.url or url
        except requests.RequestException:
            return url
        finally:
            throwaway.close()

    def _bootstrap_credentials(self) -> None:
        """Obtain the ttwid cookie Douyin requires before it will answer.

        Without ttwid the signed detail request still returns an empty body. The
        union/register endpoint hands one out anonymously, so this keeps the
        downloader usable with no user-supplied cookie.

        The registration call is made on a THROWAWAY session and only its
        cookies are kept. Verified the hard way: issuing it on the API session
        poisons that session permanently — every subsequent signed request comes
        back HTTP 200 with a zero-byte body, while a fresh session carrying the
        exact same cookies succeeds every time. `Connection: close` does not fix
        it, so this is server-side state tied to the connection, not a header.
        """
        if self._bootstrapped:
            return
        self._bootstrapped = True
        if self._cookie:
            for part in self._cookie.split(";"):
                if "=" in part:
                    name, _, value = part.partition("=")
                    self._session.cookies.set(name.strip(), value.strip())
        if self._session.cookies.get("ttwid"):
            return
        # Best-effort: an explicit DOUYIN_COOKIE may already carry what's needed,
        # and _get_detail reports a clear error if this turns out to matter.
        with contextlib.suppress(requests.RequestException):
            throwaway = requests.Session()
            throwaway.headers.update({"User-Agent": _UA})
            throwaway.post(
                _TTWID_API,
                json={
                    "region": "cn",
                    "aid": 1768,
                    "needFid": False,
                    "service": "www.ixigua.com",
                    "migrate_info": {"ticket": "", "source": "node"},
                    "cbUrlProtocol": "https",
                    "union": True,
                },
                timeout=20,
            )
            for name, value in throwaway.cookies.items():
                self._session.cookies.set(name, value)
            throwaway.close()

    def _get_detail(self, url: str) -> dict[str, Any]:
        aweme_id = extract_aweme_id(self._resolve(url))
        self._bootstrap_credentials()

        ms_token = self._session.cookies.get("msToken") or _gen_ms_token()
        params = {**_BASE_PARAMS, "aweme_id": aweme_id, "msToken": ms_token}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
            signed_query, _ab, ua, _body = ABogus(
                fp=fingerprint, user_agent=_UA
            ).generate_abogus(query, "")
        except Exception as exc:  # noqa: BLE001 - signing is third-party code
            raise DownloadError(f"Failed to sign Douyin API request: {exc}") from exc

        try:
            resp = self._session.get(
                f"{_DETAIL_API}?{signed_query}",
                headers={"User-Agent": ua},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise DownloadError(f"Douyin API request failed: {exc}") from exc

        # An empty 200 is Douyin's anti-bot rejection — there is no error body.
        if not resp.content:
            raise DownloadError(
                "Douyin returned an empty response (anti-bot rejection). Set "
                "DOUYIN_COOKIE to a browser cookie string and retry."
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise DownloadError(f"Douyin API returned non-JSON content: {exc}") from exc

        detail = payload.get("aweme_detail")
        if not isinstance(detail, dict):
            raise DownloadError(
                f"Douyin API returned no video detail for {aweme_id!r} "
                f"(status_code={payload.get('status_code')!r}). The video may be "
                "private, deleted, or region-locked."
            )
        return detail

    @staticmethod
    def _video_url(detail: dict[str, Any]) -> str:
        """First usable stream URL, preferring h264 for downstream ffmpeg work."""
        video = detail.get("video")
        if not isinstance(video, dict):
            return ""
        for key in _STREAM_KEYS:
            source = video.get(key)
            if not isinstance(source, dict):
                continue
            for candidate in source.get("url_list") or []:
                if isinstance(candidate, str) and candidate.startswith("http"):
                    return candidate
        return ""

    def _download_stream(self, url: str, out_path: Path, *, max_retries: int = 3) -> None:
        """Stream the video to disk, resuming from a partial file on retry."""
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            done = out_path.stat().st_size if out_path.exists() else 0
            headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}
            if done:
                headers["Range"] = f"bytes={done}-"
            try:
                with self._session.get(
                    url, headers=headers, stream=True, timeout=60
                ) as resp:
                    if done and resp.status_code == 200:
                        # Server ignored the Range header; restart cleanly.
                        done = 0
                    elif resp.status_code not in (200, 206):
                        raise DownloadError(
                            f"Douyin stream returned HTTP {resp.status_code}"
                        )
                    mode = "ab" if done else "wb"
                    with open(out_path, mode) as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            if chunk:
                                fh.write(chunk)
                if out_path.exists() and out_path.stat().st_size > 0:
                    return
                last_error = DownloadError("Downloaded file is empty")
            except (requests.RequestException, DownloadError) as exc:
                last_error = exc
            if attempt == max_retries:
                break
        raise DownloadError(f"Failed to download Douyin video: {last_error}")

    @staticmethod
    def _build_metadata(url: str, detail: dict[str, Any]) -> VideoMetadata:
        raw_video = detail.get("video")
        raw_author = detail.get("author")
        raw_stats = detail.get("statistics")
        video: dict[str, Any] = raw_video if isinstance(raw_video, dict) else {}
        author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
        stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}

        def _as_int(value: object) -> int | None:
            try:
                return int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        # Douyin reports duration in milliseconds, unlike the other sites.
        duration_ms = video.get("duration") or detail.get("duration")
        duration = (duration_ms / 1000.0) if isinstance(duration_ms, (int, float)) else None

        desc = (detail.get("desc") or "").strip()
        tags = re.findall(r"#(\S+)", desc)
        # The caption doubles as the title; strip hashtags so the workspace
        # directory name stays readable, and fall back to the id.
        title = re.sub(r"#\S+", "", desc).strip() or str(detail.get("aweme_id") or "")

        cover = ""
        raw_cover = video.get("cover")
        cover_src: dict[str, Any] = raw_cover if isinstance(raw_cover, dict) else {}
        for candidate in cover_src.get("url_list") or []:
            if isinstance(candidate, str) and candidate:
                cover = candidate
                break

        create_time = detail.get("create_time")
        return VideoMetadata(
            video_id=str(detail.get("aweme_id") or ""),
            title=title,
            source_url=url,
            duration=duration,
            uploader=str(author.get("nickname") or ""),
            uploader_id=str(author.get("sec_uid") or ""),
            upload_date=str(create_time or ""),
            description=desc,
            tags=tags,
            cover_image_url=cover,
            like_count=_as_int(stats.get("digg_count")),
            favorite_count=_as_int(stats.get("collect_count")),
            download_timestamp=datetime.now(timezone.utc).isoformat(),
        )
