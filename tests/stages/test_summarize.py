from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from forge_video_summarizer.errors import SummarizationError
from forge_video_summarizer.models import Transcript, TranscriptSegment, VideoMetadata
from forge_video_summarizer.stages.summarize import (
    build_prompt,
    summarize_transcript,
)


def _transcript():
    return Transcript(
        segments=[
            TranscriptSegment(0.0, 2.0, "Intro topic."),
            TranscriptSegment(65.0, 3.0, "Second topic."),
        ],
        locale="en-US",
        full_text="Intro topic. Second topic.",
    )


def test_build_prompt_includes_context_and_anchors():
    meta = VideoMetadata(video_id="BV1", title="My Talk", duration=120)
    prompt = build_prompt(_transcript(), meta)
    assert "My Talk" in prompt
    assert "2.0 minutes" in prompt
    assert "en-US" in prompt
    assert "[00:00] Intro topic." in prompt
    assert "[01:05] Second topic." in prompt


def test_build_prompt_no_metadata():
    prompt = build_prompt(_transcript(), None)
    assert "[00:00] Intro topic." in prompt


class FakeResponses:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response):
        self.responses = FakeResponses(response)


def test_summarize_success_output_text(config):
    resp = MagicMock()
    resp.output_text = "# Summary\nGreat content."
    client = FakeClient(resp)

    out = summarize_transcript(_transcript(), config, metadata=None, client=client)
    assert out == "# Summary\nGreat content."
    # correct model + instructions passed
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-sol"
    assert "teacher" in call["instructions"].lower()


def test_summarize_extracts_from_output_blocks(config):
    # No output_text; nested output/content blocks (dict form).
    resp = MagicMock(spec=["output"])
    resp.output = [{"content": [{"text": "Part1 "}, {"text": "Part2"}]}]
    client = FakeClient(resp)
    out = summarize_transcript(_transcript(), config, client=client)
    assert out == "Part1 Part2"


def test_summarize_empty_transcript_raises(config):
    with pytest.raises(SummarizationError, match="empty"):
        summarize_transcript(Transcript(), config, client=FakeClient(MagicMock()))


def test_summarize_sdk_error_wrapped(config):
    class Boom:
        class responses:
            @staticmethod
            def create(**kw):
                raise RuntimeError("network down")

    with pytest.raises(SummarizationError, match="failed"):
        summarize_transcript(_transcript(), config, client=Boom())


def test_summarize_unextractable_response_raises(config):
    resp = MagicMock(spec=["output"])
    resp.output = []
    client = FakeClient(resp)
    with pytest.raises(SummarizationError, match="extract"):
        summarize_transcript(_transcript(), config, client=client)
