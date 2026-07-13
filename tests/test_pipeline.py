from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from forge_video_summarizer.errors import DownloadError
from forge_video_summarizer.models import (
    Transcript,
    TranscriptSegment,
    VideoMetadata,
)
from forge_video_summarizer.pipeline import Pipeline, _looks_like_url
from forge_video_summarizer.workspace import Workspace


def test_looks_like_url():
    assert _looks_like_url("https://x")
    assert _looks_like_url("http://x")
    assert not _looks_like_url("/home/me/video.mp4")
    assert not _looks_like_url("BV123")


def test_run_download_writes_metadata(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    meta = VideoMetadata(video_id="BV1", title="Vid", source_url="u")

    fake_dl = MagicMock()
    fake_dl.fetch_metadata.return_value = meta

    def do_download(url, dest):
        (dest / "video.mp4").write_text("v")
        from forge_video_summarizer.downloaders.base import DownloadResult
        return DownloadResult(video_path=dest / "video.mp4", metadata=meta)

    fake_dl.download.side_effect = do_download

    with patch("forge_video_summarizer.pipeline.get_downloader", return_value=fake_dl):
        ws = pipe.run_download("https://www.bilibili.com/video/BV1")

    assert ws.metadata_path.exists()
    saved = json.loads(ws.metadata_path.read_text())
    assert saved["title"] == "Vid"


def test_run_download_cache_skips_download(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    meta = VideoMetadata(video_id="BV1", title="Vid")
    fake_dl = MagicMock()
    fake_dl.fetch_metadata.return_value = meta

    # Pre-create the workspace with an existing video.
    ws0 = Workspace(tmp_path, "Vid", "BV1")
    ws0.ensure()
    ws0.video_path("mp4").write_text("already")

    with patch("forge_video_summarizer.pipeline.get_downloader", return_value=fake_dl):
        pipe.run_download("https://www.bilibili.com/video/BV1")

    fake_dl.download.assert_not_called()


def test_workspace_for_local_copies_and_probes(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path / "out")
    local = tmp_path / "myclip.mp4"
    local.write_text("data")

    with patch("forge_video_summarizer.pipeline.probe_duration", return_value=42.0):
        ws = pipe.workspace_for_local(local)

    assert ws.find_video() is not None
    meta = json.loads(ws.metadata_path.read_text())
    assert meta["title"] == "myclip"
    assert meta["duration"] == 42.0
    assert meta["video_id"] == "local"


def test_workspace_for_local_missing(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    with pytest.raises(DownloadError):
        pipe.workspace_for_local(tmp_path / "nope.mp4")


def test_run_extract_no_video(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    with pytest.raises(DownloadError, match="No video"):
        pipe.run_extract(ws)


def test_run_transcribe_cache_hit(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    # Cache holds the RAW Azure fast-transcription response (source of truth).
    raw = {
        "combinedPhrases": [{"text": "hi"}],
        "phrases": [
            {"offsetMilliseconds": 0, "durationMilliseconds": 1000, "locale": "zh-CN", "text": "hi"}
        ],
    }
    ws.transcript_json_path.write_text(json.dumps(raw))

    with patch("forge_video_summarizer.pipeline.transcribe_audio") as tr:
        result = pipe.run_transcribe(ws, force=False)
    tr.assert_not_called()
    assert result.segments[0].text == "hi"
    assert result.locale == "zh-CN"


def test_run_transcribe_writes_artifacts(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    ws.audio_path.write_bytes(b"audio")
    t = Transcript(segments=[TranscriptSegment(0.0, 1.0, "hi")], locale="en-US")

    with patch("forge_video_summarizer.pipeline.probe_duration", return_value=10.0), \
         patch("forge_video_summarizer.pipeline.transcribe_audio", return_value=t):
        result = pipe.run_transcribe(ws)

    assert ws.transcript_json_path.exists()
    assert ws.transcript_txt_path.exists()
    assert "[00:00] hi" in ws.transcript_txt_path.read_text()
    assert result.segments[0].text == "hi"


def test_run_transcribe_saves_raw_json(config, tmp_path):
    """transcript.json must be the raw API response, not the parsed shape."""
    pipe = Pipeline(config, output_root=tmp_path)
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    ws.audio_path.write_bytes(b"audio")
    raw = {"durationMilliseconds": 5000, "phrases": [{"offsetMilliseconds": 0, "text": "hi"}]}
    t = Transcript(segments=[TranscriptSegment(0.0, 1.0, "hi")], raw=raw)

    with patch("forge_video_summarizer.pipeline.probe_duration", return_value=5.0), \
         patch("forge_video_summarizer.pipeline.transcribe_audio", return_value=t):
        pipe.run_transcribe(ws)

    saved = json.loads(ws.transcript_json_path.read_text())
    assert saved == raw  # raw response, verbatim


def test_run_summarize_cache_hit(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    ws.summary_path.write_text("cached summary")
    with patch("forge_video_summarizer.pipeline.summarize_transcript") as sm:
        out = pipe.run_summarize(ws)
    sm.assert_not_called()
    assert out.read_text() == "cached summary"


def _seed_summarize_ws(tmp_path):
    """Workspace with a cached transcript.json + metadata, ready for run_summarize."""
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    ws.transcript_json_path.write_text(json.dumps({
        "combinedPhrases": [{"text": "hi"}],
        "phrases": [
            {"offsetMilliseconds": 0, "durationMilliseconds": 1000, "locale": "zh-CN", "text": "hi"}
        ],
    }))
    ws.metadata_path.write_text(json.dumps(VideoMetadata(video_id="BV1", title="T").to_dict()))
    return ws


def test_run_summarize_writes(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = _seed_summarize_ws(tmp_path)
    with patch("forge_video_summarizer.pipeline.summarize_transcript", return_value="# S") as sm, \
         patch("forge_video_summarizer.pipeline.generate_overview_image", return_value=None):
        out = pipe.run_summarize(ws)
    assert out.read_text() == "# S"
    # metadata was passed through
    assert sm.call_args.kwargs["metadata"].title == "T"


def test_run_summarize_generates_overview_image(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = _seed_summarize_ws(tmp_path)
    with patch("forge_video_summarizer.pipeline.summarize_transcript", return_value="# S"), \
         patch("forge_video_summarizer.pipeline.generate_overview_image") as gi:
        out = pipe.run_summarize(ws)
    # summary written, and the overview step was invoked with the workspace png path
    assert out.read_text() == "# S"
    assert gi.call_args.args[2] == ws.overview_image_path


def test_run_summarize_degrades_when_overview_raises(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path)
    ws = _seed_summarize_ws(tmp_path)
    with patch("forge_video_summarizer.pipeline.summarize_transcript", return_value="# S"), \
         patch("forge_video_summarizer.pipeline.generate_overview_image",
               side_effect=RuntimeError("boom")):
        out = pipe.run_summarize(ws)
    # summary still ships even when the overview step blows up
    assert out.read_text() == "# S"


def test_run_all_local_end_to_end(config, tmp_path):
    pipe = Pipeline(config, output_root=tmp_path / "out")
    local = tmp_path / "clip.mp4"
    local.write_text("v")
    t = Transcript(segments=[TranscriptSegment(0.0, 1.0, "hi")], locale="en-US")

    with patch("forge_video_summarizer.pipeline.probe_duration", return_value=10.0), \
         patch("forge_video_summarizer.pipeline.extract_audio") as ex, \
         patch("forge_video_summarizer.pipeline.transcribe_audio", return_value=t), \
         patch("forge_video_summarizer.pipeline.summarize_transcript", return_value="# Final"), \
         patch("forge_video_summarizer.pipeline.generate_overview_image", return_value=None):
        ex.side_effect = lambda video, out, force=False: (out.write_bytes(b"a") or out)
        result = pipe.run_all(str(local))

    assert result.name == "summary.md"
    assert result.read_text() == "# Final"
