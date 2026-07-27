"""Xiaohongshu (小红书 / RedNote) VIDEO downloader — distilled from the reference
project's headless core (ONLY the video-extraction path; TUI/CLI/image/live code dropped).

Flow (no login required; an optional cookie improves reliability):
  1. Resolve a short `xhslink.com` link -> the full `/explore/<id>?xsec_token=...` URL
     (the token lives in the query string and is required to fetch the note page).
  2. GET the note page HTML with browser-like headers (UA + Referer, optional Cookie).
  3. Parse the `window.__INITIAL_STATE__` blob out of a <script> tag into JSON. XHS embeds
     JS `undefined` literals, which aren't valid JSON — we replace them with `null`.
  4. Pull the note out of `note.noteDetailMap.<id>.note` (PC) or `noteData.data.noteData`
     (phone), then the video URL:
       - preferred: `video.consumer.originVideoKey` -> https://sns-video-bd.xhscdn.com/<key>
       - fallback:  highest `video.media.stream.{h264,h265}[].masterUrl` (or a backupUrl)
  5. Download the single progressive MP4 (Referer header required or the CDN 403s).

XHS videos are a single muxed MP4 — no separate audio stream / ffmpeg mux needed (unlike
bilibili's DASH). We always download the full video (visual capability is planned).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..errors import DownloadError
from ..models import VideoMetadata
from .base import Downloader, DownloadResult

__all__ = ["XiaohongshuDownloader", "extract_note_id"]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)
_REFERER = "https://www.xiaohongshu.com/"
_VIDEO_CDN = "https://sns-video-bd.xhscdn.com/"

# URL shapes we recognize (xiaohongshu.com and its rednote.com mirror).
_HOST_RE = re.compile(r"(?:xiaohongshu\.com|rednote\.com)", re.I)
_SHORT_RE = re.compile(r"https?://xhslink\.com/\S+", re.I)
_ID_RE = re.compile(r"(?:explore|item|discovery/item)/([0-9a-zA-Z]+)")
# window.__INITIAL_STATE__={...};  (greedy to the matching close before </script>)
_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S
)


def extract_note_id(url: str) -> str:
    """Extract the 24-hex note id from an XHS explore/item/discovery URL."""
    m = _ID_RE.search(url or "")
    if not m:
        raise DownloadError(f"Could not find an XHS note id in: {url!r}")
    return m.group(1)


class XiaohongshuDownloader(Downloader):
    def __init__(self, cookie: str = "", *, session: requests.Session | None = None):
        self._cookie = cookie
        self._session = session or self._make_session(cookie)

    @staticmethod
    def _make_session(cookie: str) -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA, "Referer": _REFERER})
        if cookie:
            s.headers["Cookie"] = cookie.strip()
        return s

    # ── interface ───────────────────────────────────────────────────────
    def can_handle(self, url: str) -> bool:
        url = url or ""
        return bool(_HOST_RE.search(url) or _SHORT_RE.search(url))

    def fetch_metadata(self, url: str) -> VideoMetadata:
        full_url = self._resolve(url)
        note_id, note = self._get_note(full_url)
        return self._build_metadata(full_url, note_id, note)

    def download(self, url: str, dest_dir: Path) -> DownloadResult:
        full_url = self._resolve(url)
        note_id, note = self._get_note(full_url)
        metadata = self._build_metadata(full_url, note_id, note)

        video_url = self._video_url(note)
        if not video_url:
            raise DownloadError(
                f"No video stream in note {note_id} — is it an image/gallery post?"
            )

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "video.mp4"
        self._download_stream(video_url, out)
        return DownloadResult(video_path=out, metadata=metadata)

    # ── XHS fetch + parse ───────────────────────────────────────────────
    def _resolve(self, url: str) -> str:
        """Follow an xhslink.com short link to the real URL (keeps xsec_token)."""
        if m := _SHORT_RE.search(url or ""):
            try:
                r = self._session.get(m.group(), allow_redirects=True, timeout=10)
                return str(r.url)
            except requests.RequestException as exc:
                raise DownloadError(f"failed to resolve short link: {exc}") from exc
        return url

    def _get_note(self, url: str) -> tuple[str, dict]:
        """Fetch the note page and return (note_id, note_dict)."""
        note_id = extract_note_id(url)
        try:
            r = self._session.get(url, timeout=10)
            r.raise_for_status()
            html = r.text
        except requests.RequestException as exc:
            raise DownloadError(f"note page request failed: {exc}") from exc

        state = self._parse_initial_state(html)
        note = self._find_note(state, note_id)
        if not note:
            raise DownloadError(
                f"Could not extract note data for {note_id} "
                "(login/anti-bot wall? try setting XHS_COOKIE in .env)."
            )
        return note_id, note

    @staticmethod
    def _parse_initial_state(html: str) -> dict:
        """Parse the window.__INITIAL_STATE__ JSON blob out of the page HTML."""
        m = _STATE_RE.search(html or "")
        if not m:
            raise DownloadError("note page has no __INITIAL_STATE__ (blocked or changed?)")
        blob = m.group(1)
        # XHS embeds JS `undefined` literals — invalid JSON. Replace with null.
        blob = re.sub(r"\bundefined\b", "null", blob)
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            raise DownloadError(f"failed to parse note JSON: {exc}") from exc

    @staticmethod
    def _find_note(state: dict, note_id: str) -> dict:
        """Locate the note object across the PC and phone page shapes."""
        # PC: note.noteDetailMap.<id>.note
        note_map = (state.get("note") or {}).get("noteDetailMap") or {}
        entry = note_map.get(note_id) or (
            next(iter(note_map.values()), None) if note_map else None
        )
        if entry and entry.get("note"):
            return entry["note"]
        # Phone: noteData.data.noteData
        phone = ((state.get("noteData") or {}).get("data") or {}).get("noteData")
        return phone or {}

    @staticmethod
    def _video_url(note: dict) -> str:
        """Resolve the best single-file video URL from the note, or '' if not a video."""
        video = note.get("video") or {}
        # Preferred: originVideoKey -> CDN URL.
        key = ((video.get("consumer") or {}).get("originVideoKey")) or ""
        if key:
            return _VIDEO_CDN + key
        # Fallback: highest-resolution stream master/backup URL.
        stream = (video.get("media") or {}).get("stream") or {}
        items = [*(stream.get("h264") or []), *(stream.get("h265") or [])]
        if not items:
            return ""
        best = max(items, key=lambda x: x.get("height", 0))
        backups = best.get("backupUrls") or []
        return backups[0] if backups else best.get("masterUrl", "")

    def _download_stream(self, url: str, out_path: Path, *, max_retries: int = 5) -> None:
        """Download a progressive MP4 to out_path, resuming with HTTP Range on drops."""
        done = out_path.stat().st_size if out_path.exists() else 0
        total: int | None = None
        attempt = 0
        while True:
            headers = {"Range": f"bytes={done}-"} if done else {}
            try:
                with self._session.get(url, headers=headers, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    if total is None:
                        cr = r.headers.get("Content-Range")
                        if cr and "/" in cr:
                            total = int(cr.rsplit("/", 1)[1])
                        elif r.headers.get("Content-Length"):
                            total = int(r.headers["Content-Length"]) + done
                    with open(out_path, "ab" if done else "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 20):
                            if chunk:
                                f.write(chunk)
                                done += len(chunk)
            except requests.RequestException as exc:
                attempt += 1
                if attempt > max_retries:
                    raise DownloadError(
                        f"video download failed after retries: {exc}"
                    ) from exc
                done = out_path.stat().st_size if out_path.exists() else 0
                continue

            if total is None or done >= total:
                return
            attempt += 1
            if attempt > max_retries:
                raise DownloadError(
                    f"video download incomplete: {done}/{total} bytes after retries"
                )

    # ── metadata mapping ────────────────────────────────────────────────
    @staticmethod
    def _build_metadata(url: str, note_id: str, note: dict) -> VideoMetadata:
        user = note.get("user") or {}
        interact = note.get("interactInfo") or {}
        video = note.get("video") or {}
        # Duration is seconds in capa; some shapes carry ms.
        cap = ((video.get("media") or {}).get("video") or {})
        duration = cap.get("duration") or video.get("duration")
        tags = [
            t.get("name", "")
            for t in (note.get("tagList") or [])
            if isinstance(t, dict) and t.get("name")
        ]

        def _as_int(v: object) -> int | None:
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        time_ms = note.get("time")
        return VideoMetadata(
            video_id=note_id,
            title=note.get("title") or note_id,
            source_url=url,
            duration=float(duration) if duration else None,
            uploader=user.get("nickname") or user.get("nickName", ""),
            uploader_id=str(user.get("userId", "")),
            upload_date=str(time_ms or ""),
            description=note.get("desc", ""),
            tags=tags,
            like_count=_as_int(interact.get("likedCount")),
            favorite_count=_as_int(interact.get("collectedCount")),
            download_timestamp=datetime.now(timezone.utc).isoformat(),
        )
