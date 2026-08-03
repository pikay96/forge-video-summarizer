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

__all__ = [
    "summarize_transcript",
    "build_prompt",
    "SYSTEM_INSTRUCTIONS",
    "LANGUAGE_CHOICES",
]

# Accepted --language values. Names are spelled out for the prompt because a
# bare code ("zh") is ambiguous to the model about script and register.
_LANGUAGE_NAMES = {
    "zh": "Chinese (简体中文)",
    "en": "English",
}
LANGUAGE_CHOICES = tuple(_LANGUAGE_NAMES)

SYSTEM_INSTRUCTIONS = """\
You are an expert teacher and note-taker. You turn a video transcript into a written
summary so clear that a reader who never watched the video fully understands the topic
AND could be questioned on it and hold their own.

Write the summary in the transcript's dominant language (if the transcript mixes
languages, use whichever dominates).

Produce Markdown with these sections, IN THIS ORDER:
1. Title — the video title.
2. TL;DR — 2-3 sentences: what the video is and its single core takeaway.
3. Why it matters — brief framing: the problem/topic and who should care. END THIS SECTION
   with a short bullet list headed "This video answers:" giving the 3-5 KEY QUESTIONS the
   video sets out to answer, phrased as real questions the reader would actually type into
   a search box. They orient the reader before the detail starts, so make them the
   questions someone lands on this page hoping to resolve.
4. Key takeaways — bullet points the reader should walk away knowing. Placed BEFORE the
   walkthrough so a reader gets the payoff first and can then read on for the reasoning.
5. Walkthrough — the heart of the summary. ORGANIZE IT BY CONCEPT, NOT BY THE CLOCK.
6. Q&A — the questions this material answers, each with a concise answer, including the
   probing "why/how" questions an interviewer would ask.

HOW TO WRITE THE WALKTHROUGH — this is what separates a summary from a transcript:

- Each `###` heading names a CONCEPT, MECHANISM, or QUESTION — never a timestamp and never
  a vague label like "Part 2" or "The speaker continues". Good headings read like a
  textbook's: name the mechanism being explained, the question being answered, or the
  trade-off being weighed.
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


def build_prompt(
    transcript: Transcript,
    metadata: VideoMetadata | None,
    *,
    language: str | None = None,
) -> str:
    """Assemble the user input: title/duration context + timestamped transcript.

    `language` overrides the default "dominant language" behaviour. It is stated
    as an explicit instruction at the TOP of the prompt (not appended) so it is
    read before the transcript, which is what makes it stick against a long
    transcript in the other language.
    """
    title = metadata.title if metadata else ""
    parts = []
    if language:
        label = _LANGUAGE_NAMES.get(language, language)
        parts.append(
            f"OUTPUT LANGUAGE: Write the ENTIRE summary in {label}, including every "
            "heading, bullet, and the Q&A — regardless of what language the transcript "
            "is in. Translate the content; do not merely transcribe it."
        )
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
    language: str | None = None,
) -> str:
    """Summarize a transcript into markdown. `client` injectable for testing.

    `language` (e.g. "zh" / "en") forces the output language; None keeps the
    default of following the transcript's dominant language.
    """
    config.require_openai()
    if not transcript.segments:
        raise SummarizationError("Transcript is empty; nothing to summarize")

    client = client or make_client(config)
    prompt = build_prompt(transcript, metadata, language=language)
    summary = call_responses(client, config, SYSTEM_INSTRUCTIONS, prompt)
    if not summary:
        raise SummarizationError("Could not extract text from the model response")
    return summary
