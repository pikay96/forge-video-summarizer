from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from forge_video_summarizer.errors import AudioTooLongError, TranscriptionError
from forge_video_summarizer.stages.transcribe import (
    _build_definition,
    parse_response,
    transcribe_audio,
)

# ── parse_response (raw Azure fast-transcription response → Transcript) ──────

RAW = {
    "durationMilliseconds": 40000,
    "combinedPhrases": [{"text": "欢迎各位。记忆很重要。"}],
    "phrases": [
        {"offsetMilliseconds": 280, "durationMilliseconds": 15000, "locale": "zh-CN", "text": "欢迎各位。"},
        {"offsetMilliseconds": 15600, "durationMilliseconds": 3000, "locale": "zh-CN", "text": "记忆很重要。"},
    ],
}


def test_parse_response_uses_real_offsets():
    t = parse_response(RAW)
    assert len(t.segments) == 2
    assert t.segments[0].start == pytest.approx(0.28)
    assert t.segments[1].start == pytest.approx(15.6)
    assert t.locale == "zh-CN"
    assert t.approximate_timestamps is False
    assert t.full_text == "欢迎各位。记忆很重要。"


def test_parse_response_skips_empty_and_derives_full_text():
    payload = {"phrases": [
        {"offsetMilliseconds": 0, "durationMilliseconds": 1000, "text": "a"},
        {"offsetMilliseconds": 1000, "durationMilliseconds": 1000, "text": "  "},
        {"offsetMilliseconds": 2000, "durationMilliseconds": 1000, "text": "b"},
    ]}
    t = parse_response(payload)
    assert [s.text for s in t.segments] == ["a", "b"]
    assert t.full_text == "ab"  # no combinedPhrases -> joined from segments


def test_parse_response_empty():
    assert parse_response({}).segments == []


# ── _build_definition ───────────────────────────────────────────────────────

def test_definition_has_locales_no_enhanced_mode(config):
    d = _build_definition(config)
    assert d["locales"] == ["zh-CN", "en-US"]
    assert "enhancedMode" not in d  # plain fast transcription


def test_definition_blank_languages_falls_back(config):
    config.speech_languages = "  "
    assert _build_definition(config)["locales"] == ["zh-CN", "en-US"]


# ── transcribe_audio (mocked HTTP) ──────────────────────────────────────────

def _session_returning(status, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    sess = MagicMock()
    sess.post.return_value = resp
    return sess


def test_transcribe_missing_file(config, tmp_path):
    with pytest.raises(TranscriptionError, match="not found"):
        transcribe_audio(tmp_path / "nope.wav", config)


def test_transcribe_success(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    sess = _session_returning(200, RAW)
    t = transcribe_audio(audio, config, session=sess)
    assert len(t.segments) == 2
    assert t.segments[0].start == pytest.approx(0.28)
    assert t.locale == "zh-CN"
    assert t.raw == RAW  # raw response retained as source of truth
    # sent multipart audio + a definition with locales, no enhancedMode
    kwargs = sess.post.call_args.kwargs
    import json as _json
    definition = _json.loads(kwargs["data"]["definition"])
    assert definition["locales"] == ["zh-CN", "en-US"]
    assert "enhancedMode" not in definition


def test_transcribe_api_error(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    sess = _session_returning(400, text="bad locale")
    with pytest.raises(TranscriptionError, match="returned 400"):
        transcribe_audio(audio, config, session=sess)


def test_transcribe_no_speech(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    sess = _session_returning(200, {"phrases": []})
    with pytest.raises(TranscriptionError, match="no speech"):
        transcribe_audio(audio, config, session=sess)


def test_transcribe_network_error(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    sess = MagicMock()
    sess.post.side_effect = requests.RequestException("boom")
    with pytest.raises(TranscriptionError, match="request failed"):
        transcribe_audio(audio, config, session=sess)


def test_transcribe_rejects_oversized_file(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"0123456789")  # 10 bytes
    with patch("forge_video_summarizer.stages.transcribe.MAX_AUDIO_BYTES", 5):
        with pytest.raises(AudioTooLongError, match="MB"):
            transcribe_audio(audio, config)


def test_transcribe_rejects_overlong_duration(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    with pytest.raises(AudioTooLongError, match="h,"):
        transcribe_audio(audio, config, duration=3 * 60 * 60)
