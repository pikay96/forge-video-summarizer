from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge_video_summarizer.config import Config
from forge_video_summarizer.downloaders import get_downloader
from forge_video_summarizer.downloaders.bilibili import BilibiliDownloader, normalize_bvid
from forge_video_summarizer.errors import DownloadError


class FakeResp:
    def __init__(self, payload=None, *, content=b"", status=200, headers=None):
        self._payload = payload
        self._content = content
        self.status_code = status
        self.headers = {"Content-Length": str(len(content))}
        if headers:
            self.headers.update(headers)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def iter_content(self, chunk_size=1):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


VIEW_PAYLOAD = {
    "code": 0,
    "data": {
        "title": "Test Video",
        "duration": 120,
        "owner": {"name": "Author", "mid": 42},
        "stat": {"view": 1000, "like": 50, "coin": 10, "favorite": 5},
        "desc": "a description",
        "tname": "Tech",
        "pic": "https://img/cover.jpg",
        "pubdate": 1600000000,
        "pages": [{"cid": 999, "page": 1}],
    },
}

PLAYURL_PAYLOAD = {
    "code": 0,
    "data": {
        "dash": {
            "video": [{"baseUrl": "https://cdn/video.m4s"}],
            "audio": [{"baseUrl": "https://cdn/audio.m4s"}],
        }
    },
}


def test_normalize_bvid_from_url():
    assert normalize_bvid("https://www.bilibili.com/video/BV1xx411c7mD/") == "BV1xx411c7mD"


def test_normalize_bvid_raw():
    assert normalize_bvid("BV1abc") == "BV1abc"


def test_normalize_bvid_invalid():
    with pytest.raises(DownloadError):
        normalize_bvid("no id here")


def test_can_handle():
    dl = BilibiliDownloader()
    assert dl.can_handle("https://www.bilibili.com/video/BV1")
    assert dl.can_handle("BV1abc")
    assert not dl.can_handle("https://youtube.com/watch?v=x")


def test_fetch_metadata_maps_fields():
    session = MagicMock()
    session.get.return_value = FakeResp(VIEW_PAYLOAD)
    dl = BilibiliDownloader(session=session)
    meta = dl.fetch_metadata("BV1xx")
    assert meta.title == "Test Video"
    assert meta.uploader == "Author"
    assert meta.uploader_id == "42"
    assert meta.view_count == 1000
    assert meta.coin_count == 10
    assert meta.tags == ["Tech"]
    assert meta.cover_image_url == "https://img/cover.jpg"
    assert meta.duration == 120


def test_view_api_error_raises():
    session = MagicMock()
    session.get.return_value = FakeResp({"code": -404, "message": "not found"})
    dl = BilibiliDownloader(session=session)
    with pytest.raises(DownloadError, match="view API error"):
        dl.fetch_metadata("BV1xx")


def test_playurl_no_dash_raises():
    session = MagicMock()
    session.get.side_effect = [
        FakeResp(VIEW_PAYLOAD),
        FakeResp({"code": 0, "data": {"dash": {"video": [], "audio": []}}}),
    ]
    dl = BilibiliDownloader(session=session)
    with pytest.raises(DownloadError, match="no DASH"):
        dl.download("BV1xx", __import__("pathlib").Path("/tmp/nope_xyz"))


def test_download_full_flow(tmp_path):
    session = MagicMock()
    # view, playurl, then two stream GETs (context-manager form)
    session.get.side_effect = [
        FakeResp(VIEW_PAYLOAD),
        FakeResp(PLAYURL_PAYLOAD),
        FakeResp(content=b"videodata"),
        FakeResp(content=b"audiodata"),
    ]
    dl = BilibiliDownloader(session=session)

    with patch("forge_video_summarizer.downloaders.bilibili.subprocess.run") as run:
        # Simulate ffmpeg producing the output file.
        def fake_run(cmd, **kw):
            # output path is second-to-last arg before -y
            output = cmd[-2]
            __import__("pathlib").Path(output).write_text("merged")
            return MagicMock(returncode=0)

        run.side_effect = fake_run
        result = dl.download("https://www.bilibili.com/video/BV1xx", tmp_path)

    assert result.video_path.name == "video.mp4"
    assert result.video_path.exists()
    assert result.metadata.title == "Test Video"
    # temp stream files cleaned up
    assert not (tmp_path / "_video.m4s").exists()
    assert not (tmp_path / "_audio.m4s").exists()


def test_get_downloader_selects_bilibili():
    cfg = Config(bili_sessdata="s")
    dl = get_downloader("https://www.bilibili.com/video/BV1", cfg)
    assert isinstance(dl, BilibiliDownloader)


def test_build_downloaders_returns_list():
    from forge_video_summarizer.downloaders import build_downloaders
    dls = build_downloaders(Config())
    assert len(dls) == 1
    assert isinstance(dls[0], BilibiliDownloader)


def test_view_api_network_error():
    session = MagicMock()
    import requests as _rq
    session.get.side_effect = _rq.RequestException("boom")
    dl = BilibiliDownloader(session=session)
    with pytest.raises(DownloadError, match="request failed"):
        dl.fetch_metadata("BV1xx")


def test_download_no_pages():
    session = MagicMock()
    payload = {"code": 0, "data": {"title": "T", "pages": []}}
    session.get.return_value = FakeResp(payload)
    dl = BilibiliDownloader(session=session)
    with pytest.raises(DownloadError, match="No parts"):
        dl.download("BV1xx", __import__("pathlib").Path("/tmp/x"))


def test_stream_download_error(tmp_path):
    import requests as _rq
    session = MagicMock()
    session.get.side_effect = [
        FakeResp(VIEW_PAYLOAD),
        FakeResp(PLAYURL_PAYLOAD),
    ] + [_rq.RequestException("net")] * 10  # exhaust retries
    dl = BilibiliDownloader(session=session)
    with pytest.raises(DownloadError, match="stream download failed after retries"):
        dl.download("https://www.bilibili.com/video/BV1", tmp_path)


def test_stream_download_resumes_after_drop(tmp_path):
    """First GET delivers a partial body, retry (Range) delivers the rest."""
    from requests.exceptions import ChunkedEncodingError

    dl = BilibiliDownloader()
    out = tmp_path / "stream.m4s"
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=60):
        calls.append(headers or {})
        if len(calls) == 1:
            # Announce total=10 via Content-Length, but the body raises mid-read.
            r = FakeResp(content=b"", headers={"Content-Length": "10"})

            def broken_iter(chunk_size=1):
                yield b"AAAAA"  # 5 of 10 bytes
                raise ChunkedEncodingError("dropped")

            r.iter_content = broken_iter
            return r
        # Resume: Range header present; deliver remaining 5 bytes.
        assert headers and headers["Range"] == "bytes=5-"
        return FakeResp(content=b"BBBBB", headers={"Content-Range": "bytes 5-9/10"})

    dl._session = MagicMock()
    dl._session.get.side_effect = fake_get
    dl._download_stream(
        "https://cdn/stream", out
    )
    assert out.read_bytes() == b"AAAAABBBBB"
    assert len(calls) == 2  # dropped once, resumed once


def test_merge_ffmpeg_error(tmp_path):
    import subprocess
    session = MagicMock()
    session.get.side_effect = [
        FakeResp(VIEW_PAYLOAD),
        FakeResp(PLAYURL_PAYLOAD),
        FakeResp(content=b"v"),
        FakeResp(content=b"a"),
    ]
    dl = BilibiliDownloader(session=session)
    with patch(
        "forge_video_summarizer.downloaders.bilibili.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "ffmpeg"),
    ), pytest.raises(DownloadError, match="merge failed"):
        dl.download("https://www.bilibili.com/video/BV1", tmp_path)


def test_make_session_sets_cookie():
    dl = BilibiliDownloader(sessdata="abc;")
    assert dl._session.cookies.get("SESSDATA") == "abc"
