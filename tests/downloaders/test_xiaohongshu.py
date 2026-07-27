from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from forge_video_summarizer.config import Config
from forge_video_summarizer.downloaders import get_downloader
from forge_video_summarizer.downloaders.xiaohongshu import (
    XiaohongshuDownloader,
    extract_note_id,
)
from forge_video_summarizer.errors import DownloadError


class FakeResp:
    def __init__(self, *, text="", content=b"", status=200, headers=None, url=""):
        self._text = text
        self._content = content
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {"Content-Length": str(len(content))}
        if headers:
            self.headers.update(headers)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _state_html(note_id: str, note: dict) -> str:
    """Build a note page HTML with the __INITIAL_STATE__ blob (PC shape)."""
    state = {"note": {"noteDetailMap": {note_id: {"note": note}}}}
    blob = json.dumps(state, ensure_ascii=False)
    return f"<html><body><script>window.__INITIAL_STATE__={blob}</script></body></html>"


def _video_note(note_id="653abc", *, key="spectrum/xyz.mp4"):
    return {
        "noteId": note_id,
        "type": "video",
        "title": "泡沫测试",
        "desc": "描述文本",
        "user": {"nickname": "作者A", "userId": "u123"},
        "interactInfo": {"likedCount": "1200", "collectedCount": "300"},
        "tagList": [{"name": "AI"}, {"name": "投资"}],
        "time": 1700000000000,
        "video": {
            "consumer": {"originVideoKey": key},
            "media": {"video": {"duration": 95}},
        },
    }


# ── extract_note_id / can_handle ────────────────────────────────────────────

def test_extract_note_id_from_explore():
    assert extract_note_id("https://www.xiaohongshu.com/explore/653abc?xsec_token=X") == "653abc"


def test_extract_note_id_from_discovery_item():
    assert extract_note_id("https://www.xiaohongshu.com/discovery/item/deadBEEF?x=1") == "deadBEEF"


def test_extract_note_id_missing_raises():
    with pytest.raises(DownloadError, match="note id"):
        extract_note_id("https://www.xiaohongshu.com/user/profile/abc")


def test_can_handle():
    dl = XiaohongshuDownloader()
    assert dl.can_handle("https://www.xiaohongshu.com/explore/x?xsec_token=Y")
    assert dl.can_handle("https://xhslink.com/a/abc")
    assert dl.can_handle("https://www.rednote.com/explore/x")
    assert not dl.can_handle("https://www.bilibili.com/video/BV1")
    assert not dl.can_handle("")


def test_registry_routes_xhs_urls():
    cfg = Config()
    assert isinstance(
        get_downloader("https://www.xiaohongshu.com/explore/x?xsec_token=Y", cfg),
        XiaohongshuDownloader,
    )
    assert isinstance(get_downloader("https://xhslink.com/a/abc", cfg), XiaohongshuDownloader)


# ── initial-state parsing ───────────────────────────────────────────────────

def test_parse_initial_state_handles_js_undefined():
    html = (
        "<script>window.__INITIAL_STATE__="
        '{"note":{"noteDetailMap":{},"x":undefined}}</script>'
    )
    state = XiaohongshuDownloader._parse_initial_state(html)
    assert state["note"]["x"] is None  # undefined -> null


def test_parse_initial_state_missing_blob_raises():
    with pytest.raises(DownloadError, match="__INITIAL_STATE__"):
        XiaohongshuDownloader._parse_initial_state("<html>no state here</html>")


def test_find_note_phone_shape():
    note = {"noteId": "p1", "video": {"consumer": {"originVideoKey": "k"}}}
    state = {"noteData": {"data": {"noteData": note}}}
    assert XiaohongshuDownloader._find_note(state, "p1") == note


# ── video URL resolution ────────────────────────────────────────────────────

def test_video_url_prefers_origin_key():
    url = XiaohongshuDownloader._video_url(_video_note(key="foo/bar.mp4"))
    assert url == "https://sns-video-bd.xhscdn.com/foo/bar.mp4"


def test_video_url_falls_back_to_highest_stream():
    note = {"video": {"media": {"stream": {
        "h264": [
            {"height": 720, "masterUrl": "http://cdn/720.mp4"},
            {"height": 1080, "masterUrl": "http://cdn/1080.mp4"},
        ],
    }}}}
    assert XiaohongshuDownloader._video_url(note) == "http://cdn/1080.mp4"


def test_video_url_prefers_backup_when_present():
    note = {"video": {"media": {"stream": {"h264": [
        {"height": 1080, "masterUrl": "http://m", "backupUrls": ["http://backup"]},
    ]}}}}
    assert XiaohongshuDownloader._video_url(note) == "http://backup"


def test_video_url_empty_for_image_post():
    assert XiaohongshuDownloader._video_url({"video": {}}) == ""


# ── metadata ────────────────────────────────────────────────────────────────

def test_build_metadata_maps_fields():
    note = _video_note("653abc")
    md = XiaohongshuDownloader._build_metadata(
        "https://www.xiaohongshu.com/explore/653abc", "653abc", note
    )
    assert md.video_id == "653abc"
    assert md.title == "泡沫测试"
    assert md.uploader == "作者A"
    assert md.uploader_id == "u123"
    assert md.duration == 95.0
    assert md.like_count == 1200
    assert md.favorite_count == 300
    assert md.tags == ["AI", "投资"]


# ── fetch_metadata / download (mocked HTTP) ─────────────────────────────────

def test_fetch_metadata(tmp_path):
    note = _video_note("653abc")
    sess = MagicMock()
    sess.get.return_value = FakeResp(text=_state_html("653abc", note))
    dl = XiaohongshuDownloader(session=sess)
    md = dl.fetch_metadata("https://www.xiaohongshu.com/explore/653abc?xsec_token=X")
    assert md.title == "泡沫测试" and md.video_id == "653abc"


def test_download_writes_video(tmp_path):
    note = _video_note("653abc", key="v/clip.mp4")
    payload = b"FAKEMP4DATA" * 100
    sess = MagicMock()

    def _get(url, **kwargs):
        if "xhscdn.com" in url:  # the video stream
            return FakeResp(content=payload)
        return FakeResp(text=_state_html("653abc", note))  # the note page

    sess.get.side_effect = _get
    dl = XiaohongshuDownloader(session=sess)
    result = dl.download(
        "https://www.xiaohongshu.com/explore/653abc?xsec_token=X", tmp_path
    )
    assert result.video_path == tmp_path / "video.mp4"
    assert result.video_path.read_bytes() == payload
    assert result.metadata.video_id == "653abc"


def test_download_image_post_raises(tmp_path):
    note = {"noteId": "img1", "type": "normal", "title": "图文", "video": {}}
    sess = MagicMock()
    sess.get.return_value = FakeResp(text=_state_html("img1", note))
    dl = XiaohongshuDownloader(session=sess)
    with pytest.raises(DownloadError, match="No video stream"):
        dl.download("https://www.xiaohongshu.com/explore/img1?xsec_token=X", tmp_path)


def test_short_link_resolved(tmp_path):
    note = _video_note("653abc")
    sess = MagicMock()

    def _get(url, **kwargs):
        if "xhslink.com" in url:
            return FakeResp(
                url="https://www.xiaohongshu.com/explore/653abc?xsec_token=REAL"
            )
        return FakeResp(text=_state_html("653abc", note))

    sess.get.side_effect = _get
    dl = XiaohongshuDownloader(session=sess)
    md = dl.fetch_metadata("https://xhslink.com/a/short")
    assert md.video_id == "653abc"


def test_note_page_blocked_raises(tmp_path):
    sess = MagicMock()
    sess.get.return_value = FakeResp(text="<html>login wall</html>")
    dl = XiaohongshuDownloader(session=sess)
    with pytest.raises(DownloadError, match="__INITIAL_STATE__"):
        dl.fetch_metadata("https://www.xiaohongshu.com/explore/x1?xsec_token=X")
