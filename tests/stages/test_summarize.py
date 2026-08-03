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


# ── forced output language (--language) ─────────────────────────────────────

def _mini_transcript():
    from forge_video_summarizer.models import Transcript, TranscriptSegment
    return Transcript(
        segments=[TranscriptSegment(start=0.0, duration=2.0, text="hello world")],
        locale="en-US",
    )


def test_prompt_has_no_language_directive_by_default():
    """Default must stay 'dominant language' — no override leaking in."""
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None)
    assert "OUTPUT LANGUAGE" not in prompt


def test_prompt_states_language_first_when_forced():
    """The directive must precede the transcript, or a long transcript in the
    other language drowns it out."""
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None, language="zh")
    assert prompt.startswith("OUTPUT LANGUAGE")
    assert "简体中文" in prompt
    assert prompt.index("OUTPUT LANGUAGE") < prompt.index("TRANSCRIPT")


def test_prompt_language_english():
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None, language="en")
    assert "in English" in prompt


def test_language_choices_exposed_for_the_cli():
    from forge_video_summarizer.stages.summarize import LANGUAGE_CHOICES
    assert set(LANGUAGE_CHOICES) == {"zh", "en"}


def test_summarize_transcript_passes_language_through(monkeypatch):
    from forge_video_summarizer.config import Config
    from forge_video_summarizer.stages import summarize as mod

    seen = {}

    def fake_call(client, config, system, prompt):
        seen["prompt"] = prompt
        return "# out"

    monkeypatch.setattr(mod, "call_responses", fake_call)
    cfg = Config(openai_endpoint="https://x", openai_key="k")
    mod.summarize_transcript(_mini_transcript(), cfg, client=object(), language="zh")
    assert seen["prompt"].startswith("OUTPUT LANGUAGE")
    assert "简体中文" in seen["prompt"]


def test_forced_language_asks_for_original_terms_in_parens():
    """Translation can blur technical terms, so keep the source term alongside."""
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None, language="zh")
    assert "TERMINOLOGY" in prompt
    assert "translated(original)" in prompt
    # first occurrence only — otherwise the summary reads as a bilingual transcript
    assert "ONCE" in prompt
    # ordinary vocabulary must not get annotated
    assert "Do NOT annotate ordinary words" in prompt


def test_terminology_rule_absent_without_forced_language():
    """No override -> output is already in the source language, nothing to gloss."""
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None)
    assert "TERMINOLOGY" not in prompt


def test_terminology_example_is_not_video_specific():
    """Standing rule: no prompt content tuned to one particular video."""
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None, language="zh")
    assert "glycemic" not in prompt.lower()


def test_forced_language_translates_the_tldr_heading():
    """"TL;DR" is an English literal in the section spec, so it survives
    translation unless called out — the other headings translate, leaving one
    inconsistent English heading."""
    from forge_video_summarizer.stages.summarize import build_prompt
    prompt = build_prompt(_mini_transcript(), None, language="zh")
    assert "HEADINGS" in prompt
    assert "TL;DR" in prompt.split("HEADINGS")[1]
