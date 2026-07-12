"""Stage 3 — Transcription via Azure Speech Services fast transcription.

POST {endpoint}/speechtotext/transcriptions:transcribe (multipart audio + a
`definition` JSON). Enhanced mode (MAI, verbatim), single locale. Transcript stays
in the original language.

MAI-transcribe returns the whole audio as ONE phrase, so per-segment timestamps are
derived by sentence-splitting + proportional interpolation (see parse_response).

v1 does NOT chunk: if the audio exceeds the fast-transcription limits
(500 MB / 5 h) we raise AudioTooLongError rather than truncate. The chunk +
per-chunk-offset + stitch design is the documented future extension point.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from ..config import Config
from ..errors import AudioTooLongError, TranscriptionError
from ..models import Transcript, TranscriptSegment

__all__ = ["transcribe_audio", "parse_response", "MAX_AUDIO_BYTES", "MAX_AUDIO_SECONDS"]

_API_VERSION = "2025-10-15"
# Azure Speech fast transcription limits (Standard S0), verified from MS Learn.
MAX_AUDIO_BYTES = 500 * 1024 * 1024  # < 500 MB
MAX_AUDIO_SECONDS = 5 * 60 * 60  # < 5 hours


def _build_definition(config: Config) -> dict:
    """Match the reference request: no `locales` (MAI auto-detects the language),
    phraseList present-but-empty (wired for per-run domain terms), enhanced MAI mode.
    Sending `locales` with enhanced mode is rejected ("requires at most one locale"),
    so we omit it and let MAI identify the language automatically.
    """
    return {
        "phraseList": {"phrases": []},
        "enhancedMode": {
            "enabled": True,
            "model": config.speech_model,
            "transcribeStyle": "verbatim",
        },
    }


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on CJK + ASCII end punctuation, keeping the mark."""
    parts = re.split(r"(?<=[。！？；.!?;])\s*", text.strip())
    return [p for p in (s.strip() for s in parts) if p]


def parse_response(payload: dict) -> Transcript:
    """Map an Azure fast-transcription JSON response into a Transcript.

    MAI-transcribe (enhanced mode) returns the whole audio as a SINGLE phrase, so
    real per-phrase timestamps aren't available. To still provide ``[MM:SS]`` anchors,
    we split that phrase into sentences and interpolate each sentence's start time
    proportionally by character offset across the total duration. Timestamps are
    therefore APPROXIMATE (speech rate ~constant) but monotonic and close enough for
    topic anchors. When the API does return multiple phrases, we use them directly.
    """
    phrases = payload.get("phrases") or []
    locale = phrases[0].get("locale", "") if phrases else ""

    combined = payload.get("combinedPhrases") or []
    full_text = (combined[0].get("text") or "").strip() if combined else ""

    segments: list[TranscriptSegment] = []
    if len(phrases) > 1:
        # Genuine multi-phrase response: use real timestamps.
        for ph in phrases:
            text = (ph.get("text") or "").strip()
            if text:
                segments.append(
                    TranscriptSegment(
                        start=float(ph.get("offsetMilliseconds", 0)) / 1000.0,
                        duration=float(ph.get("durationMilliseconds", 0)) / 1000.0,
                        text=text,
                    )
                )
    elif phrases:
        # Single phrase (MAI): derive sentence segments with interpolated timestamps.
        ph = phrases[0]
        base = float(ph.get("offsetMilliseconds", 0)) / 1000.0
        total_ms = float(
            ph.get("durationMilliseconds") or payload.get("durationMilliseconds") or 0
        )
        total_s = total_ms / 1000.0
        text = (ph.get("text") or "").strip()
        full_text = full_text or text
        sentences = _split_sentences(text)
        if sentences and total_s > 0:
            total_chars = sum(len(s) for s in sentences)
            cursor = 0
            for i, sent in enumerate(sentences):
                start = base + total_s * (cursor / total_chars)
                cursor += len(sent)
                nxt = base + total_s * (cursor / total_chars)
                end = nxt if i < len(sentences) - 1 else base + total_s
                segments.append(
                    TranscriptSegment(start=start, duration=max(0.0, end - start), text=sent)
                )
        elif text:
            segments.append(TranscriptSegment(start=base, duration=total_s, text=text))

    if not full_text:
        full_text = " ".join(s.text for s in segments)

    return Transcript(segments=segments, locale=locale, full_text=full_text)


def _check_limits(audio_path: Path, duration: float | None) -> None:
    size = audio_path.stat().st_size
    if size >= MAX_AUDIO_BYTES:
        raise AudioTooLongError(
            f"Audio is {size / 1024 / 1024:.0f} MB, exceeding the "
            f"{MAX_AUDIO_BYTES // 1024 // 1024} MB fast-transcription limit. "
            "v1 does not chunk; use a shorter source."
        )
    if duration is not None and duration >= MAX_AUDIO_SECONDS:
        raise AudioTooLongError(
            f"Audio is {duration / 3600:.1f} h, exceeding the 5 h "
            "fast-transcription limit. v1 does not chunk; use a shorter source."
        )


def transcribe_audio(
    audio_path: str | Path,
    config: Config,
    *,
    duration: float | None = None,
    session: requests.Session | None = None,
) -> Transcript:
    """Transcribe an audio file, returning a Transcript. Raises on limits/errors."""
    config.require_speech()
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise TranscriptionError(f"Audio file not found: {audio_path}")

    _check_limits(audio_path, duration)

    endpoint = config.speech_endpoint.rstrip("/")
    url = f"{endpoint}/speechtotext/transcriptions:transcribe?api-version={_API_VERSION}"
    definition = _build_definition(config)

    sess = session or requests.Session()
    try:
        with open(audio_path, "rb") as fh:
            resp = sess.post(
                url,
                headers={"Ocp-Apim-Subscription-Key": config.speech_key},
                files={"audio": (audio_path.name, fh, "audio/mpeg")},
                data={"definition": json.dumps(definition)},
                timeout=600,
            )
    except requests.RequestException as exc:
        raise TranscriptionError(f"transcription request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise TranscriptionError(
            f"transcription API returned {resp.status_code}: {resp.text[:300]}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise TranscriptionError(f"invalid JSON from transcription API: {exc}") from exc

    return parse_response(payload)
