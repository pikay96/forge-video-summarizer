"""Stage 3 — Transcription via Azure Speech *fast transcription* (REST, synchronous).

POST {endpoint}/speechtotext/transcriptions:transcribe (multipart: audio + a
`definition` JSON). Plain fast transcription (NO enhancedMode) returns multiple
phrases each with a REAL offset + duration, and supports multi-locale language ID
via `locales`. That gives us accurate per-segment timestamps AND keeps the original
language — in a single fast call (~34s for a 27-min video).

NOTE: enhancedMode/MAI is intentionally NOT used — it collapses the whole audio into
one phrase (no usable per-segment timing) and rejects multi-locale `locales`.

transcript.json stores the raw Azure response (real offsets — source of truth);
parse_response maps it into a Transcript.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from ..config import Config
from ..errors import AudioTooLongError, TranscriptionError
from ..models import Transcript, TranscriptSegment

__all__ = ["transcribe_audio", "parse_response", "MAX_AUDIO_BYTES", "MAX_AUDIO_SECONDS"]

_API_VERSION = "2024-11-15"
# Azure Speech fast transcription limits (verified from MS Learn).
MAX_AUDIO_BYTES = 300 * 1024 * 1024  # < 300 MB per request
MAX_AUDIO_SECONDS = 2 * 60 * 60  # < 2 hours


def _build_definition(config: Config) -> dict:
    """Fast transcription request definition: candidate locales for language ID.
    No enhancedMode (which would collapse output to one phrase and reject multiple
    locales). Empty/blank config falls back to zh-CN + en-US candidates.
    """
    locales = [x.strip() for x in config.speech_languages.split(",") if x.strip()]
    return {"locales": locales or ["zh-CN", "en-US"]}


def parse_response(payload: dict) -> Transcript:
    """Map an Azure fast-transcription JSON response into a Transcript.

    Plain fast transcription returns a `phrases` array, each with a real
    `offsetMilliseconds` / `durationMilliseconds` and a detected `locale`. We use
    those directly — no interpolation, timestamps are exact.
    """
    phrases = payload.get("phrases") or []
    locale = phrases[0].get("locale", "") if phrases else ""

    segments: list[TranscriptSegment] = []
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

    combined = payload.get("combinedPhrases") or []
    full_text = (combined[0].get("text") or "").strip() if combined else ""
    if not full_text:
        full_text = "".join(s.text for s in segments)

    return Transcript(
        segments=segments,
        locale=locale,
        full_text=full_text,
        approximate_timestamps=False,  # real per-phrase offsets
        raw=payload,
    )


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
            f"Audio is {duration / 3600:.1f} h, exceeding the 2 h "
            "fast-transcription limit. v1 does not chunk; use a shorter source."
        )


def transcribe_audio(
    audio_path: str | Path,
    config: Config,
    *,
    duration: float | None = None,
    session: requests.Session | None = None,
) -> Transcript:
    """Transcribe an audio file via fast transcription, returning a Transcript with
    REAL per-segment timestamps. Raises on limits/errors.
    """
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
                files={"audio": (audio_path.name, fh, "audio/wav")},
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

    transcript = parse_response(payload)
    if not transcript.segments:
        raise TranscriptionError("no speech recognized in audio")
    return transcript
