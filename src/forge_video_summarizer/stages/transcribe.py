"""Stage 3 — Transcription via Azure Speech Services fast transcription.

POST {endpoint}/speechtotext/transcriptions:transcribe (multipart audio + a
`definition` JSON). Enhanced mode (verbatim), empty-but-wired phrase list,
automatic language identification. Transcript stays in the original language.

v1 does NOT chunk: if the audio exceeds the fast-transcription limits
(500 MB / 5 h) we raise AudioTooLongError rather than truncate. The chunk +
per-chunk-offset + stitch design is the documented future extension point.
"""

from __future__ import annotations

import json
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

# Candidate locales for language identification (mixed EN/CN content).
_DEFAULT_LOCALES = ["zh-CN", "en-US"]


def _build_definition(config: Config, locales: list[str]) -> dict:
    return {
        "locales": locales,
        "phraseList": {"phrases": []},  # empty but wired for per-run domain terms
        "enhancedMode": {
            "enabled": True,
            "model": config.speech_model,
            "transcribeStyle": "verbatim",
        },
    }


def parse_response(payload: dict) -> Transcript:
    """Map an Azure fast-transcription JSON response into a Transcript.

    Uses phrase-level timestamps (offset + duration in milliseconds), which are
    already referenced to the original audio timeline.
    """
    phrases = payload.get("phrases") or []
    segments: list[TranscriptSegment] = []
    for ph in phrases:
        start = float(ph.get("offsetMilliseconds", 0)) / 1000.0
        duration = float(ph.get("durationMilliseconds", 0)) / 1000.0
        text = (ph.get("text") or "").strip()
        if text:
            segments.append(TranscriptSegment(start=start, duration=duration, text=text))

    locale = ""
    if phrases:
        locale = phrases[0].get("locale", "") or ""

    combined = payload.get("combinedPhrases") or []
    if combined:
        full_text = (combined[0].get("text") or "").strip()
    else:
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
    locales: list[str] | None = None,
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
    definition = _build_definition(config, locales or _DEFAULT_LOCALES)

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
