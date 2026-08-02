"""Tests for the Douyin downloader.

Network is never touched: the detail API is faked with a payload shaped like a
real `aweme_detail` response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_video_summarizer.downloaders import DouyinDownloader, get_downloader
from forge_video_summarizer.downloaders.douyin import extract_aweme_id
from forge_video_summarizer.errors import DownloadError

AWEME_ID = "7604129988555574538"

DETAIL = {
    "aweme_id": AWEME_ID,
    "desc": "一部充满力量的逆袭小故事10！ #亚述家族 #影娱漫谈编辑部",
    "create_time": 1764500000,
    "author": {"nickname": "冒牌毒舌", "sec_uid": "MS4wLjABAAAA_fake"},
    "statistics": {"digg_count": 1931, "collect_count": 42},
    "video": {
        "duration": 816875,  # milliseconds, unlike the other sites
        "play_addr_h264": {"url_list": ["https://v3-web.douyinvod.com/h264.mp4"]},
        "play_addr": {"url_list": ["https://v3-web.douyinvod.com/default.mp4"]},
        "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpg"]},
    },
}


class _FakeResp:
    def __init__(self, payload=None, *, content=b"", status=200):
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else content
        self.status_code = status
        self.url = f"https://www.douyin.com/video/{AWEME_ID}"

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload


class _FakeSession:
    """Minimal stand-in; records calls so we can assert on bootstrap behaviour."""

    def __init__(self, payload=None, *, content=b""):
        self._payload = payload
        self._content = content
        self.headers: dict[str, str] = {}
        self.cookies = _FakeCookies()
        self.posts: list[str] = []
        self.gets: list[str] = []

    def post(self, url, **kw):
        self.posts.append(url)
        return _FakeResp({})

    def get(self, url, **kw):
        self.gets.append(url)
        return _FakeResp(self._payload, content=self._content)

    def close(self):
        pass


class _FakeCookies(dict):
    def get(self, name, default=None):
        return dict.get(self, name, default)

    def set(self, name, value, **kw):
        self[name] = value

    def keys(self):
        return dict.keys(self)


# ── URL handling ────────────────────────────────────────────────────────────

def test_can_handle_douyin_urls():
    d = DouyinDownloader()
    assert d.can_handle("https://www.douyin.com/video/7604129988555574538")
    assert d.can_handle("https://v.douyin.com/iRNBho6G/")
    assert d.can_handle("https://www.iesdouyin.com/share/video/123/")


def test_can_handle_rejects_other_platforms():
    """Must not steal URLs belonging to the bilibili or Xiaohongshu downloaders."""
    d = DouyinDownloader()
    assert not d.can_handle("https://b23.tv/tL2n2mM")
    assert not d.can_handle("https://xhslink.cn/o/abc")
    assert not d.can_handle("https://www.bilibili.com/video/BV1xx")
    assert not d.can_handle("")


def test_extract_aweme_id_from_url_shapes():
    assert extract_aweme_id(f"https://www.douyin.com/video/{AWEME_ID}") == AWEME_ID
    assert extract_aweme_id(f"https://www.douyin.com/note/{AWEME_ID}") == AWEME_ID
    assert extract_aweme_id(f"https://www.douyin.com/?modal_id={AWEME_ID}") == AWEME_ID
    assert extract_aweme_id(f"https://www.douyin.com/video/{AWEME_ID}?x=1") == AWEME_ID


def test_extract_aweme_id_raises_without_an_id():
    with pytest.raises(DownloadError):
        extract_aweme_id("https://www.douyin.com/discover")


def test_registry_routes_douyin_urls():
    from forge_video_summarizer.config import Config

    dl = get_downloader(f"https://www.douyin.com/video/{AWEME_ID}", Config())
    assert isinstance(dl, DouyinDownloader)


# ── metadata ────────────────────────────────────────────────────────────────

def test_fetch_metadata_maps_fields():
    d = DouyinDownloader(session=_FakeSession({"aweme_detail": DETAIL}))
    md = d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")
    assert md.video_id == AWEME_ID
    assert md.uploader == "冒牌毒舌"
    assert md.like_count == 1931
    assert md.favorite_count == 42


def test_duration_is_converted_from_milliseconds():
    """Douyin reports ms; the rest of the pipeline assumes seconds."""
    d = DouyinDownloader(session=_FakeSession({"aweme_detail": DETAIL}))
    md = d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")
    assert md.duration == pytest.approx(816.875)


def test_title_strips_hashtags_and_keeps_them_as_tags():
    """The caption doubles as the title, so trailing #tags would pollute the dir name."""
    d = DouyinDownloader(session=_FakeSession({"aweme_detail": DETAIL}))
    md = d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")
    assert md.title == "一部充满力量的逆袭小故事10！"
    assert "#" not in md.title
    assert md.tags == ["亚述家族", "影娱漫谈编辑部"]


def test_title_falls_back_to_id_when_desc_is_only_hashtags():
    detail = {**DETAIL, "desc": "#onlytags"}
    d = DouyinDownloader(session=_FakeSession({"aweme_detail": detail}))
    md = d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")
    assert md.title == AWEME_ID


# ── stream selection ────────────────────────────────────────────────────────

def test_prefers_h264_stream():
    """h264 decodes predictably in ffmpeg downstream; h265 variants do not."""
    assert DouyinDownloader._video_url(DETAIL) == "https://v3-web.douyinvod.com/h264.mp4"


def test_falls_back_through_stream_keys():
    detail = {"video": {"play_addr": {"url_list": ["https://x/default.mp4"]}}}
    assert DouyinDownloader._video_url(detail) == "https://x/default.mp4"


def test_video_url_empty_for_image_posts():
    assert DouyinDownloader._video_url({"video": {}}) == ""
    assert DouyinDownloader._video_url({}) == ""


def test_download_rejects_image_posts(tmp_path: Path):
    detail = {**DETAIL, "video": {"duration": 1000}}
    d = DouyinDownloader(session=_FakeSession({"aweme_detail": detail}))
    with pytest.raises(DownloadError, match="image/gallery"):
        d.download(f"https://www.douyin.com/video/{AWEME_ID}", tmp_path)


# ── anti-bot failure modes ──────────────────────────────────────────────────

def test_empty_response_raises_actionable_error():
    """Douyin signals anti-bot rejection with HTTP 200 and a ZERO-BYTE body."""
    d = DouyinDownloader(session=_FakeSession(None, content=b""))
    with pytest.raises(DownloadError, match="DOUYIN_COOKIE"):
        d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")


def test_missing_detail_reports_why():
    d = DouyinDownloader(session=_FakeSession({"status_code": 2145}))
    with pytest.raises(DownloadError, match="private, deleted, or region-locked"):
        d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")


def test_ttwid_bootstrap_does_not_touch_the_api_session():
    """Regression: posting to ttwid on the API session poisons it permanently.

    Every later signed request then returns 200 with an empty body, while a
    fresh session carrying identical cookies succeeds. So the registration must
    happen on a throwaway session — assert the API session issues no POST.
    """
    session = _FakeSession({"aweme_detail": DETAIL})
    d = DouyinDownloader(session=session)
    d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")
    assert session.posts == []


def test_explicit_cookie_is_loaded_into_the_session():
    session = _FakeSession({"aweme_detail": DETAIL})
    d = DouyinDownloader(cookie="ttwid=abc; sessionid=xyz", session=session)
    d.fetch_metadata(f"https://www.douyin.com/video/{AWEME_ID}")
    assert session.cookies.get("ttwid") == "abc"
    assert session.cookies.get("sessionid") == "xyz"


def test_short_link_resolution_does_not_touch_the_api_session():
    """Regression: resolving v.douyin.com on the API session breaks the API call.

    The redirect chain plants `__ac_nonce` and a *challenge* `ttwid`. That marks
    the session as pending anti-bot verification (later signed calls return an
    empty body), AND the planted ttwid made _bootstrap_credentials think it
    already had a good cookie, so it skipped fetching a real one. Net effect: a
    share link failed while the equivalent direct URL worked. Resolution must
    therefore happen on a throwaway session.
    """
    session = _FakeSession({"aweme_detail": DETAIL})
    d = DouyinDownloader(session=session)
    d._resolve("https://v.douyin.com/Na457zvLCKo/")
    assert session.gets == []          # never used the API session
    assert list(session.cookies) == []  # and planted nothing on it


def test_resolve_passes_through_canonical_urls():
    session = _FakeSession({"aweme_detail": DETAIL})
    d = DouyinDownloader(session=session)
    url = f"https://www.douyin.com/video/{AWEME_ID}"
    assert d._resolve(url) == url
    assert session.gets == []
