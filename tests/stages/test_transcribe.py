from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from forge_video_summarizer.errors import AudioTooLongError, TranscriptionError
from forge_video_summarizer.stages.transcribe import (
    _build_definition,
    _chunk_count,
    _split_sentences,
    _stitch_payloads,
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
        "phrases": [
            {"offsetMilliseconds": 0, "durationMilliseconds": 5000, "text": "整段没有句号"}
        ],
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


def test_transcribe_rejects_oversized_single_chunk(config, tmp_path):
    # A file over the byte limit whose duration doesn't trigger multi-chunk (unknown dur,
    # tiny byte count) — the per-request size guard still fires.
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"0123456789")
    with (
        patch("forge_video_summarizer.stages.transcribe.MAX_AUDIO_BYTES", 5),
        patch("forge_video_summarizer.stages.transcribe._chunk_count", return_value=1),
        pytest.raises(AudioTooLongError, match="MB"),
    ):
        transcribe_audio(audio, config)


# ── auto-chunking for long audio ────────────────────────────────────────────

def test_chunk_count_by_duration():
    # 2.1 h at the WAV byte rate; over the 2 h * 0.9 margin -> needs 2 chunks.
    from pathlib import Path
    with patch.object(Path, "stat") as st:
        st.return_value = MagicMock(st_size=100)
        assert _chunk_count(Path("x.wav"), duration=2.1 * 3600) == 2
        assert _chunk_count(Path("x.wav"), duration=30 * 60) == 1  # short -> 1
        assert _chunk_count(Path("x.wav"), duration=5 * 3600) == 3  # 5 h -> 3


def test_stitch_payloads_concatenates_full_timeline():
    a = {"combinedPhrases": [{"text": "前半段。"}], "phrases": [{"locale": "zh-CN"}]}
    b = {"combinedPhrases": [{"text": "后半段。"}]}
    merged = _stitch_payloads([a, b], total_dur=7512.0, locale="zh-CN")
    assert merged["combinedPhrases"][0]["text"] == "前半段。 后半段。"
    assert merged["durationMilliseconds"] == 7512000
    assert merged["phrases"][0]["offsetMilliseconds"] == 0
    assert merged["phrases"][0]["locale"] == "zh-CN"


def test_transcribe_auto_chunks_long_audio(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    # Two chunks, each returning its own MAI phrase; stitched into one transcript.
    chunk_a = {"durationMilliseconds": 20000, "combinedPhrases": [{"text": "第一段。开头。"}],
               "phrases": [{"offsetMilliseconds": 0, "durationMilliseconds": 20000,
                            "locale": "zh-CN", "text": "第一段。开头。"}]}
    chunk_b = {"durationMilliseconds": 20000, "combinedPhrases": [{"text": "第二段。结尾。"}],
               "phrases": [{"offsetMilliseconds": 0, "durationMilliseconds": 20000,
                            "locale": "zh-CN", "text": "第二段。结尾。"}]}
    sess = MagicMock()
    resp_a, resp_b = MagicMock(), MagicMock()
    resp_a.status_code = resp_b.status_code = 200
    resp_a.json.return_value, resp_b.json.return_value = chunk_a, chunk_b
    sess.post.side_effect = [resp_a, resp_b]

    with (
        patch("forge_video_summarizer.stages.transcribe._chunk_count", return_value=2),
        patch("forge_video_summarizer.stages.transcribe._split_audio",
              return_value=[tmp_path / "c0.wav", tmp_path / "c1.wav"]) as split,
    ):
        (tmp_path / "c0.wav").write_bytes(b"a")
        (tmp_path / "c1.wav").write_bytes(b"b")
        t = transcribe_audio(audio, config, duration=7512.0, session=sess)

    split.assert_called_once()
    assert sess.post.call_count == 2  # one request per chunk
    # stitched: text from both chunks, anchors spanning the full timeline
    assert "第一段" in t.full_text and "第二段" in t.full_text
    assert t.segments[0].start == pytest.approx(0.0)
    assert t.segments[-1].end == pytest.approx(7512.0, abs=1.0)


def test_transcribe_chunking_needs_ffmpeg(config, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    with (
        patch("forge_video_summarizer.stages.transcribe._chunk_count", return_value=2),
        patch("forge_video_summarizer.stages.transcribe.shutil.which", return_value=None),
        pytest.raises(TranscriptionError, match="ffmpeg is unavailable"),
    ):
        transcribe_audio(audio, config, duration=7512.0, session=MagicMock())
