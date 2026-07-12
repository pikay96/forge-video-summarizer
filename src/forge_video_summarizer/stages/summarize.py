"""Stage 4 — Summarization via Azure OpenAI (OpenAI SDK Responses API).

Produces a teacher-clear, interview-ready markdown summary whose length scales
with video duration, with inline [MM:SS] anchors (one per meaningful topic
shift). The summary is written in the video's dominant language.

v1 assumes the transcript fits the model's context window (no map-reduce).
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..errors import SummarizationError
from ..models import Transcript, VideoMetadata

__all__ = ["summarize_transcript", "build_prompt", "SYSTEM_INSTRUCTIONS"]

SYSTEM_INSTRUCTIONS = """\
You are an expert teacher and note-taker. You turn a video transcript into a written
summary so clear that a reader who never watched the video fully understands the topic
AND could be questioned on it and hold their own.

Write the summary in the transcript's dominant language (if the transcript mixes
languages, use whichever dominates).

Produce Markdown with these sections:
1. Title — the video title.
2. TL;DR — 2-3 sentences: what the video is and its single core takeaway.
3. Context / why it matters — brief framing: the problem/topic and who should care.
4. Walkthrough — sections by meaningful topic shift. Begin each section heading with an
   inline timestamp anchor in [MM:SS] or [HH:MM:SS] form taken from the transcript.
   EXPLAIN each idea like a teacher (convey the concept and the reasoning) — do not
   merely say "the speaker says X". Scale depth with the length of the material.
5. Key takeaways — bullet points the reader should walk away knowing.
6. Q&A / interview prep — the questions this material answers, each with a concise
   answer, including the probing "why/how" questions an interviewer would ask.

Create one anchor per meaningful topic shift — few for short videos, many for long ones.
Anchors must reference the original timeline exactly as they appear in the transcript.
"""


def build_prompt(transcript: Transcript, metadata: VideoMetadata | None) -> str:
    """Assemble the user input: title/duration context + timestamped transcript."""
    title = metadata.title if metadata else ""
    parts = []
    if title:
        parts.append(f"VIDEO TITLE: {title}")
    if metadata and metadata.duration:
        mins = metadata.duration / 60.0
        parts.append(f"VIDEO DURATION: ~{mins:.1f} minutes")
    if transcript.locale:
        parts.append(f"DETECTED LOCALE: {transcript.locale}")
    parts.append(
        "\nTRANSCRIPT (each line begins with its timestamp anchor):\n"
        + transcript.to_timestamped_text()
    )
    return "\n".join(parts)


def _extract_output_text(response: Any) -> str:
    """Pull text out of a Responses API result across SDK shapes."""
    text = getattr(response, "output_text", None)
    if text:
        return text.strip()

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        for block in content or []:
            block_text = getattr(block, "text", None)
            if block_text is None and isinstance(block, dict):
                block_text = block.get("text")
            if block_text:
                chunks.append(block_text)
    if chunks:
        return "".join(chunks).strip()
    raise SummarizationError("Could not extract text from the model response")


def _make_client(config: Config):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SummarizationError("The 'openai' package is required for summarization") from exc
    return OpenAI(base_url=config.openai_endpoint, api_key=config.openai_key)


def summarize_transcript(
    transcript: Transcript,
    config: Config,
    *,
    metadata: VideoMetadata | None = None,
    client: Any | None = None,
) -> str:
    """Summarize a transcript into markdown. `client` injectable for testing."""
    config.require_openai()
    if not transcript.segments:
        raise SummarizationError("Transcript is empty; nothing to summarize")

    client = client or _make_client(config)
    prompt = build_prompt(transcript, metadata)

    try:
        response = client.responses.create(
            model=config.openai_deployment,
            instructions=SYSTEM_INSTRUCTIONS,
            input=prompt,
        )
    except Exception as exc:  # noqa: BLE001 - surface any SDK/transport error uniformly
        raise SummarizationError(f"summarization request failed: {exc}") from exc

    return _extract_output_text(response)
