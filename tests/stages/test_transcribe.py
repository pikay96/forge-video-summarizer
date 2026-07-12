from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from forge_video_summarizer.errors import AudioTooLongError, TranscriptionError
from forge_video_summarizer.stages.transcribe import (
    MAX_AUDIO_BYTES,
    parse_response,
    transcribe_audio,
)

RESPONSE = {
    "combinedPhrases": [{"text": "Hello world. Second part."}],
    "phrases": [
        {"offsetMilliseconds": 0, "durationMilliseconds": 2000, "text": "Hello world.", "locale": "en-US"},
        {"offsetMilliseconds": 65000, "durationMilliseconds": 3000, "text": "Second part.", "locale": "en-US"},
    ],
}


def test_parse_response_segments():
    t = parse_response(RESPONSE)
    assert len(t.segments) == 2
    assert t.segments[0].start == 0.0
    assert t.segments[0].duration == 2.0
    assert t.segments[1].start == 65.0
    assert t.locale == "en-US"
    assert t.full_text == "Hello world. Second part."


def test_parse_response_skips_empty_text():
    payload = {"phrases": [{"offsetMilliseconds": 0, "durationMilliseconds": 1000, "text": "  "}]}
    t = parse_response(payload)
    assert t.segments == []


def test_parse_response_no_combined_falls_back():
    payload = {
        "phrases": [
            {"offsetMilliseconds": 0, "durationMilliseconds": 1000, "text": "a"},
            {"offsetMilliseconds": 1000, "durationMilliseconds": 1000, "text": "b"},
        ]
    }
    t = parse_response(payload)
    assert t.full_text == "a b"


def test_transcribe_missing_file(config, tmp_path):
    with pytest.raises(TranscriptionError, match="not found"):
        transcribe_audio(tmp_path / "nope.mp3", config)


def test_transcribe_too_large(config, tmp_path):
    audio = tmp_path / "audio.mp3"
    # Create a sparse file just over the size limit (no real disk use).
    with open(audio, "wb") as f:
        f.truncate(MAX_AUDIO_BYTES + 1)
    with pytest.raises(AudioTooLongError, match="MB"):
        transcribe_audio(audio, config)


def test_transcribe_too_long_duration(config, tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"x")
    with pytest.raises(AudioTooLongError, match="h,"):
        transcribe_audio(audio, config, duration=6 * 3600)


def test_transcribe_success(config, tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio-bytes")

    session = MagicMock()
    session.post.return_value = MagicMock(status_code=200, json=lambda: RESPONSE)

    t = transcribe_audio(audio, config, duration=100, session=session)
    assert len(t.segments) == 2
    # verify request shape
    _, kwargs = session.post.call_args
    assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "speech-key"
    assert "definition" in kwargs["data"]
    assert "audio" in kwargs["files"]


def test_transcribe_http_error(config, tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"x")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=400, text="bad request")
    with pytest.raises(TranscriptionError, match="400"):
        transcribe_audio(audio, config, duration=1, session=session)


def test_transcribe_definition_has_enhanced_and_empty_phraselist(config, tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"x")
    session = MagicMock()
    captured = {}

    def fake_post(url, **kw):
        captured.update(kw)
        return MagicMock(status_code=200, json=lambda: RESPONSE)

    session.post.side_effect = fake_post
    transcribe_audio(audio, config, duration=1, session=session)

    import json
    definition = json.loads(captured["data"]["definition"])
    assert definition["enhancedMode"]["enabled"] is True
    assert definition["enhancedMode"]["model"] == "mai-transcribe-1.5"
    assert definition["phraseList"]["phrases"] == []
    assert definition["locales"] == ["zh-CN", "en-US"]
