from __future__ import annotations

import pytest

from forge_video_summarizer.config import Config, load_config
from forge_video_summarizer.errors import ConfigError


def test_load_config_reads_file(tmp_path, monkeypatch):
    for k in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
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
    assert load_config(p).speech_key == "fromenv"


def test_missing_env_file_ok(tmp_path, monkeypatch):
    monkeypatch.delenv("BILI_SESSDATA", raising=False)
    assert load_config(tmp_path / "nope.env").bili_sessdata == ""


def test_require_speech_raises():
    with pytest.raises(ConfigError):
        Config().require_speech()


def test_require_openai_raises():
    with pytest.raises(ConfigError):
        Config(openai_endpoint="x").require_openai()


def test_require_passes_when_present():
    cfg = Config(speech_endpoint="e", speech_key="k", openai_endpoint="e", openai_key="k")
    cfg.require_speech()
    cfg.require_openai()
