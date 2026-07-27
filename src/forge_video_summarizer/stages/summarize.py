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
from ._openai import call_responses, make_client

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
4. Walkthrough — the heart of the summary. ORGANIZE IT BY CONCEPT, NOT BY THE CLOCK.
5. Key takeaways — bullet points the reader should walk away knowing.
6. Q&A / interview prep — the questions this material answers, each with a concise
   answer, including the probing "why/how" questions an interviewer would ask.

HOW TO WRITE THE WALKTHROUGH — this is what separates a summary from a transcript:

- Each `###` heading names a CONCEPT, MECHANISM, or QUESTION — never a timestamp and never
  a vague label like "Part 2" or "The speaker continues". Good headings read like a
  textbook's: "Why generation must cache K and V", "MHA vs MQA vs GQA: the memory/quality
  trade-off", "Deriving the KV cache formula".
- Build a LOGICAL progression: set up the problem, develop the idea, then resolve it. A
  section may pull together material the speaker scattered across different moments, and
  the order may differ from the video's order when that explains the topic better.
- EXPLAIN like a teacher: convey the concept and the reasoning behind it, define terms on
  first use, and make each idea stand on its own. Never narrate ("then the speaker says").
- Cite timestamps INLINE, as references inside the prose, in `[MM:SS]` or `[HH:MM:SS]` form
  exactly as they appear in the transcript — e.g. "...so every layer keeps its own K and V
  [02:18]." Put at least one inline anchor in each section, and add anchors wherever a
  specific claim, formula, diagram, or example is introduced. Anchors are POINTERS back to
  the video, not the organizing structure.
- Scale depth with the material: a short clip needs a few sections, a long talk many more.

COMPLETENESS — do not omit anything that matters. Before finishing, check that every
important concept, formula (write the math out), comparison, worked example, number, and
caveat the video presents appears somewhere in the summary. Losing a key comparison or a
derivation is a failure, even if the prose reads well.
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

    client = client or make_client(config)
    prompt = build_prompt(transcript, metadata)
    summary = call_responses(client, config, SYSTEM_INSTRUCTIONS, prompt)
    if not summary:
        raise SummarizationError("Could not extract text from the model response")
    return summary
