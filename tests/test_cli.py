from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forge_video_summarizer.cli import build_parser, main
from forge_video_summarizer.errors import DownloadError


def test_parser_requires_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_summarize_command_invokes_run_all(tmp_path):
    with patch("forge_video_summarizer.cli.load_config") as lc, \
         patch("forge_video_summarizer.cli.Pipeline") as P:
        lc.return_value = MagicMock()
        inst = P.return_value
        inst.run_all.return_value = tmp_path / "summary.md"
        rc = main(["summarize", "https://www.bilibili.com/video/BV1"])
    assert rc == 0
    inst.run_all.assert_called_once()


def test_download_command(tmp_path):
    with patch("forge_video_summarizer.cli.load_config"), \
         patch("forge_video_summarizer.cli.Pipeline") as P:
        inst = P.return_value
        ws = MagicMock()
        ws.dir = tmp_path
        inst.run_download.return_value = ws
        rc = main(["download", "https://www.bilibili.com/video/BV1"])
    assert rc == 0
    inst.run_download.assert_called_once()


def test_extract_command_local(tmp_path):
    with patch("forge_video_summarizer.cli.load_config"), \
         patch("forge_video_summarizer.cli.Pipeline") as P:
        inst = P.return_value
        ws = MagicMock()
        inst.workspace_for_local.return_value = ws
        inst.run_extract.return_value = tmp_path / "audio.mp3"
        rc = main(["extract", "/home/me/clip.mp4"])
    assert rc == 0
    inst.workspace_for_local.assert_called_once()
    inst.run_extract.assert_called_once()


def test_transcribe_command_url(tmp_path):
    with patch("forge_video_summarizer.cli.load_config"), \
         patch("forge_video_summarizer.cli.Pipeline") as P:
        inst = P.return_value
        ws = MagicMock()
        inst.run_download.return_value = ws
        rc = main(["transcribe", "https://www.bilibili.com/video/BV1"])
    assert rc == 0
    inst.run_download.assert_called_once()
    inst.run_extract.assert_called_once()
    inst.run_transcribe.assert_called_once()


def test_summarize_transcript_command(tmp_path):
    with patch("forge_video_summarizer.cli.load_config"), \
         patch("forge_video_summarizer.cli.Pipeline") as P:
        inst = P.return_value
        ws = MagicMock()
        inst.workspace_for_local.return_value = ws
        inst.run_summarize.return_value = tmp_path / "summary.md"
        rc = main(["summarize-transcript", "/home/me/clip.mp4"])
    assert rc == 0
    inst.run_summarize.assert_called_once()


def test_error_returns_exit_code_1(capsys):
    with patch("forge_video_summarizer.cli.load_config"), \
         patch("forge_video_summarizer.cli.Pipeline") as P:
        inst = P.return_value
        inst.run_all.side_effect = DownloadError("boom")
        rc = main(["summarize", "https://www.bilibili.com/video/BV1"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err


def test_module_entrypoint_runs():
    import runpy

    with patch("forge_video_summarizer.cli.main", return_value=0):
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("forge_video_summarizer", run_name="__main__")
    assert exc.value.code == 0
