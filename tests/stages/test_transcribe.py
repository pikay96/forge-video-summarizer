from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from forge_video_summarizer.errors import AudioTooLongError, TranscriptionError
from forge_video_summarizer.stages.transcribe import (
    _build_definition,
    _split_sentences,
    parse_response,
    transcribe_audio,
)

# MAI enhancedMode returns ONE phrase for the whole audio.
MAI_RAW = {
    "durationMilliseconds": 40000,
    "combinedPhrases": [{"text": "欢迎各位。记忆很重要。那么我们开始。"}],
    "phrases": [
        {"offsetMilliseconds": 0, "durationMilliseconds": 40000, "locale": "zh-CN",
         "text": "欢迎各位。记忆很重要。那么我们开始。"},
    ],
}


# ── _split_sentences ────────────────────────────────────────────────────────

def test_split_sentences_cjk_and_ascii():
    assert _split_sentences("你好。世界！ok?done.") == ["你好。", "世界！", "ok?", "done."]


# ── parse_response (single MAI phrase -> interpolated segments) ──────────────

def test_parse_response_interpolates_single_phrase():
    t = parse_response(MAI_RAW)
    assert len(t.segments) == 3  # split into 3 sentences
    assert t.approximate_timestamps is True  # interpolated, flagged
    assert t.segments[0].start == pytest.approx(0.0)
    # monotonic increasing starts
    assert t.segments[0].start < t.segments[1].start < t.segments[2].start
    # last segment ends at total duration
    assert t.segments[-1].end == pytest.approx(40.0, abs=0.01)
    assert t.locale == "zh-CN"
    assert t.full_text == "欢迎各位。记忆很重要。那么我们开始。"


def test_parse_response_single_sentence_not_approximate():
    payload = {
        "durationMilliseconds": 5000,
        "phrases": [{"offsetMilliseconds": 0, "durationMilliseconds": 5000, "text": "整段没有句号"}],
    }
    t = parse_response(payload)
    assert len(t.segments) == 1
    assert t.approximate_timestamps is False  # one block, nothing interpolated


def test_parse_response_multi_phrase_uses_real_offsets():
    payload = {"phrases": [
        {"offsetMilliseconds": 280, "durationMilliseconds": 1000, "text": "a"},
        {"offsetMilliseconds": 2000, "durationMilliseconds": 1000, "text": "b"},
    ]}
    t = parse_response(payload)
    assert t.approximate_timestamps is False
    assert t.segments[0].start == pytest.approx(0.28)
    assert t.segments[1].start == pytest.approx(2.0)


def test_parse_response_empty():
    assert parse_response({}).segments == []


# ── _build_definition ───────────────────────────────────────────────────────

def test_definition_enhanced_mode_no_locales(config):
    d = _build_definition(config)
    assert d["enhancedMode"]["enabled"] is True
    assert d["enhancedMode"]["model"] == "mai-transcribe-1.5"
    assert d["enhancedMode"]["transcribeStyle"] == "verbatim"
    assert "locales" not in d  # enhancedMode auto-detects, rejects locale lists
    assert d["phraseList"] == {"phrases": []}


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
    sess = _session_returning(200, MAI_RAW)
    t = transcribe_audio(audio, config, session=sess)
    assert len(t.segments) == 3
    assert t.approximate_timestamps is True
    assert t.raw == MAI_RAW  # raw response retained as source of truth
    import json as _json
    definition = _json.loads(sess.post.call_args.kwargs["data"]["definition"])
    assert definition["enhancedMode"]["model"] == "mai-transcribe-1.5"
    assert "locales" not in definition


def test_transcribe_api_error(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    sess = _session_returning(400, text="requires at most one locale")
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
    audio.write_bytes(b"0123456789")
    with patch("forge_video_summarizer.stages.transcribe.MAX_AUDIO_BYTES", 5):
        with pytest.raises(AudioTooLongError, match="MB"):
            transcribe_audio(audio, config)


def test_transcribe_rejects_overlong_duration(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    with pytest.raises(AudioTooLongError, match="h,"):
        transcribe_audio(audio, config, duration=3 * 60 * 60)
