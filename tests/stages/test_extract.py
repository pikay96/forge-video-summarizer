from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from forge_video_summarizer.errors import ExtractionError
from forge_video_summarizer.stages.extract import extract_audio, probe_duration

_MOD = "forge_video_summarizer.stages.extract"


def test_extract_missing_video(tmp_path):
    with pytest.raises(ExtractionError, match="not found"):
        extract_audio(tmp_path / "nope.mp4", tmp_path / "audio.mp3")


def test_extract_cache_hit(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_text("v")
    audio = tmp_path / "audio.mp3"
    audio.write_text("cached")
    with patch(f"{_MOD}.subprocess.run") as run:
        result = extract_audio(video, audio, force=False)
    run.assert_not_called()
    assert result == audio


def test_extract_runs_ffmpeg(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_text("v")
    audio = tmp_path / "audio.mp3"

    def fake_run(cmd, **kw):
        audio.write_text("extracted")
        return MagicMock()

    with patch("forge_video_summarizer.stages.extract.subprocess.run", side_effect=fake_run) as run:
        result = extract_audio(video, audio)
    run.assert_called_once()
    cmd = run.call_args[0][0]
    assert "-ar" in cmd and "16000" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert "pcm_s16le" in cmd  # WAV output for the Speech SDK
    assert result.exists()


def test_extract_force_overwrites(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_text("v")
    audio = tmp_path / "audio.mp3"
    audio.write_text("old")

    def fake_run(cmd, **kw):
        audio.write_text("new")
        return MagicMock()

    with patch("forge_video_summarizer.stages.extract.subprocess.run", side_effect=fake_run) as run:
        extract_audio(video, audio, force=True)
    run.assert_called_once()
    assert audio.read_text() == "new"


def test_extract_ffmpeg_failure(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_text("v")
    with patch(
        "forge_video_summarizer.stages.extract.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "ffmpeg"),
    ), pytest.raises(ExtractionError):
        extract_audio(video, tmp_path / "audio.mp3")


def test_extract_ffmpeg_permission_error(tmp_path):
    """A non-executable ffmpeg raises PermissionError (OSError) -> clean error."""
    video = tmp_path / "video.mp4"
    video.write_text("v")
    with patch(
        "forge_video_summarizer.stages.extract.subprocess.run",
        side_effect=PermissionError(13, "Permission denied"),
    ), pytest.raises(ExtractionError, match="failed"):
        extract_audio(video, tmp_path / "audio.mp3")


def test_extract_missing_output(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_text("v")
    with (
        patch(f"{_MOD}.subprocess.run", return_value=MagicMock()),
        pytest.raises(ExtractionError, match="no output"),
    ):
        extract_audio(video, tmp_path / "audio.mp3")


def test_probe_duration_parses():
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/ffprobe"),
        patch(f"{_MOD}.subprocess.run") as run,
    ):
        run.return_value = MagicMock(stdout="123.45\n")
        assert probe_duration("x.mp3") == 123.45


def test_probe_duration_no_ffprobe():
    with patch("forge_video_summarizer.stages.extract.shutil.which", return_value=None):
        assert probe_duration("x.mp3") is None


def test_probe_duration_bad_output():
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/ffprobe"),
        patch(f"{_MOD}.subprocess.run") as run,
    ):
        run.return_value = MagicMock(stdout="N/A\n")
        assert probe_duration("x.mp3") is None
