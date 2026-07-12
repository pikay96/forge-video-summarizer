from __future__ import annotations

import pytest

from forge_video_summarizer.config import Config, load_config, load_env_file
from forge_video_summarizer.errors import ConfigError


def test_load_env_file_parses(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "\n"
        "export FOO=bar\n"
        'QUOTED="hello world"\n'
        "SINGLE='x'\n"
        "NOEQ\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    values = load_env_file(p)
    assert values["FOO"] == "bar"
    assert values["QUOTED"] == "hello world"
    assert values["SINGLE"] == "x"
    assert values["EMPTY"] == ""
    assert "NOEQ" not in values


def test_load_env_file_missing_returns_empty(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_load_config_reads_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AZURE_SPEECH_KEY=k\nAZURE_SPEECH_ENDPOINT=https://s/\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.speech_key == "k"
    assert cfg.speech_endpoint == "https://s/"
    assert cfg.speech_model == "mai-transcribe-1.5"  # default


def test_process_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("AZURE_SPEECH_KEY=fromfile\n", encoding="utf-8")
    monkeypatch.setenv("AZURE_SPEECH_KEY", "fromenv")
    cfg = load_config(p)
    assert cfg.speech_key == "fromenv"


def test_require_speech_raises_when_missing():
    with pytest.raises(ConfigError):
        Config().require_speech()


def test_require_openai_raises_when_missing():
    with pytest.raises(ConfigError):
        Config(openai_endpoint="x").require_openai()


def test_require_passes_when_present():
    cfg = Config(
        speech_endpoint="e", speech_key="k",
        openai_endpoint="e", openai_key="k",
    )
    cfg.require_speech()
    cfg.require_openai()
