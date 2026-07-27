"""Stage 3 — Transcription via Azure Speech fast transcription, enhancedMode (MAI).

POST {endpoint}/speechtotext/transcriptions:transcribe (multipart: audio + a
`definition` JSON). enhancedMode with the MAI model (verbatim) gives the best text
quality — it recovers inline English/technical terms (e.g. "trade off", "LLM", "RAG")
and adds proper sentence punctuation, clearly better than plain fast transcription on
mixed zh/en content.

The tradeoff: MAI returns the whole audio as ONE phrase (offset 0, total duration),
so there are no real per-segment timestamps. We derive `[MM:SS]` anchors by splitting
that phrase into sentences and interpolating each start proportionally by character
offset across the total duration. These anchors are APPROXIMATE (assume ~constant
speech rate) but monotonic and close enough for topic navigation — transcript.txt
carries a header saying so, and transcript.json keeps the raw response as truth.

enhancedMode rejects multi-locale `locales` ("requires at most one locale") and
auto-detects the language, so we send no `locales` field.

Long audio: fast transcription caps at 2 h / 300 MB per request. Audio over either limit
is automatically split into equal chunks (each under the limits), transcribed separately,
and stitched back into one transcript spanning the full timeline — so a 2 h+ video Just
Works without the caller doing anything.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from ..config import Config
from ..errors import AudioTooLongError, TranscriptionError
from ..models import Transcript, TranscriptSegment

__all__ = ["transcribe_audio", "parse_response", "MAX_AUDIO_BYTES", "MAX_AUDIO_SECONDS"]

_API_VERSION = "2025-10-15"
# Azure Speech fast transcription limits (verified from MS Learn).
MAX_AUDIO_BYTES = 300 * 1024 * 1024  # < 300 MB per request
MAX_AUDIO_SECONDS = 2 * 60 * 60  # < 2 hours
# Keep each chunk comfortably under the hard limits (boundary safety margin).
_CHUNK_MARGIN = 0.9
# 16 kHz mono s16le PCM WAV = 32000 bytes/sec — used to estimate duration if ffprobe is absent.
_WAV_BYTES_PER_SEC = 16000 * 2


def _build_definition(config: Config) -> dict:
    """enhancedMode (MAI, verbatim). No `locales` — enhancedMode auto-detects the
    language and rejects a multi-locale list. phraseList present-but-empty (wired for
    per-run domain terms).
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
    """Map an Azure fast-transcription response into a Transcript.

    MAI enhancedMode returns a single phrase for the whole audio. We split it into
    sentences and interpolate per-sentence start times proportionally by character
    offset across the total duration -> APPROXIMATE but monotonic anchors. If a
    response ever has multiple phrases (non-enhanced), we use their real offsets.
    """
    phrases = payload.get("phrases") or []
    locale = phrases[0].get("locale", "") if phrases else ""

    combined = payload.get("combinedPhrases") or []
    full_text = (combined[0].get("text") or "").strip() if combined else ""

    segments: list[TranscriptSegment] = []
    approximate = False
    if len(phrases) > 1:
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
        ph = phrases[0]
        base = float(ph.get("offsetMilliseconds", 0)) / 1000.0
        total_ms = float(
            ph.get("durationMilliseconds") or payload.get("durationMilliseconds") or 0
        )
        total_s = total_ms / 1000.0
        text = (ph.get("text") or "").strip()
        full_text = full_text or text
        sentences = _split_sentences(text)
        if len(sentences) > 1 and total_s > 0:
            approximate = True  # interpolated, not measured
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
        full_text = "".join(s.text for s in segments)

    return Transcript(
        segments=segments,
        locale=locale,
        full_text=full_text,
        approximate_timestamps=approximate,
        raw=payload,
    )


def _check_size_limit(audio_path: Path) -> None:
    """Byte-size guard for a SINGLE request. Duration is handled by chunking."""
    size = audio_path.stat().st_size
    if size >= MAX_AUDIO_BYTES:
        raise AudioTooLongError(
            f"Audio chunk is {size / 1024 / 1024:.0f} MB, exceeding the "
            f"{MAX_AUDIO_BYTES // 1024 // 1024} MB fast-transcription limit."
        )


def _chunk_count(audio_path: Path, duration: float | None) -> int:
    """How many equal chunks are needed so each is under BOTH the time and byte limits."""
    size = audio_path.stat().st_size
    dur = duration if duration is not None else size / _WAV_BYTES_PER_SEC
    by_time = dur / (MAX_AUDIO_SECONDS * _CHUNK_MARGIN)
    by_size = size / (MAX_AUDIO_BYTES * _CHUNK_MARGIN)
    return max(1, math.ceil(max(by_time, by_size)))


def _split_audio(audio_path: Path, parts: int, total_dur: float, out_dir: Path) -> list[Path]:
    """Split a WAV into `parts` equal time slices via ffmpeg (stream copy). Returns paths."""
    if shutil.which("ffmpeg") is None:
        raise TranscriptionError(
            "Audio exceeds the fast-transcription limit and ffmpeg is unavailable to chunk it."
        )
    slice_dur = total_dur / parts
    chunks: list[Path] = []
    for i in range(parts):
        out = out_dir / f"chunk_{i:02d}.wav"
        cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", str(audio_path),
            "-ss", f"{i * slice_dur:.3f}", "-t", f"{slice_dur:.3f}",
            "-c", "copy", str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise TranscriptionError(f"ffmpeg failed to split audio: {exc}") from exc
        if not out.is_file() or out.stat().st_size == 0:
            raise TranscriptionError(f"ffmpeg produced no output for chunk {i}")
        chunks.append(out)
    return chunks


def _stitch_payloads(payloads: list[dict], total_dur: float, locale: str) -> dict:
    """Merge per-chunk MAI responses into ONE payload spanning the full duration.

    Each MAI chunk returns a single combined phrase; we concatenate the text and present
    it as one phrase over the whole timeline so parse_response interpolates anchors across
    the entire video (matching the single-request behavior).
    """
    texts: list[str] = []
    for p in payloads:
        combined = p.get("combinedPhrases") or []
        text = (combined[0].get("text") or "").strip() if combined else ""
        if not text:
            phrases = p.get("phrases") or []
            text = " ".join((ph.get("text") or "").strip() for ph in phrases).strip()
        if text:
            texts.append(text)
    full_text = " ".join(texts).strip()
    total_ms = int(total_dur * 1000)
    return {
        "durationMilliseconds": total_ms,
        "combinedPhrases": [{"text": full_text}],
        "phrases": [{
            "offsetMilliseconds": 0,
            "durationMilliseconds": total_ms,
            "locale": locale or "zh-CN",
            "text": full_text,
        }],
    }


def _post_transcription(audio_path: Path, config: Config, session: requests.Session) -> dict:
    """One transcription request for a within-limits audio file. Returns the raw payload."""
    _check_size_limit(audio_path)
    endpoint = config.speech_endpoint.rstrip("/")
    url = f"{endpoint}/speechtotext/transcriptions:transcribe?api-version={_API_VERSION}"
    definition = _build_definition(config)
    try:
        with open(audio_path, "rb") as fh:
            resp = session.post(
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
        return resp.json()
    except ValueError as exc:
        raise TranscriptionError(f"invalid JSON from transcription API: {exc}") from exc


def transcribe_audio(
    audio_path: str | Path,
    config: Config,
    *,
    duration: float | None = None,
    session: requests.Session | None = None,
) -> Transcript:
    """Transcribe an audio file via MAI enhancedMode, returning a Transcript.

    Audio that exceeds the fast-transcription time/size limit is automatically split into
    equal chunks (each safely under the limits), transcribed separately, and stitched into a
    single transcript spanning the full timeline. Timestamps are interpolated (approximate).
    """
    config.require_speech()
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise TranscriptionError(f"Audio file not found: {audio_path}")

    sess = session or requests.Session()
    parts = _chunk_count(audio_path, duration)

    if parts == 1:
        payload = _post_transcription(audio_path, config, sess)
    else:
        total_dur = duration if duration is not None else (
            audio_path.stat().st_size / _WAV_BYTES_PER_SEC
        )
        with tempfile.TemporaryDirectory(prefix="fvs_chunks_") as tmp:
            chunks = _split_audio(audio_path, parts, total_dur, Path(tmp))
            payloads = [_post_transcription(c, config, sess) for c in chunks]
        locale = next(
            (
                (p.get("phrases") or [{}])[0].get("locale", "")
                for p in payloads if p.get("phrases")
            ),
            "",
        )
        payload = _stitch_payloads(payloads, total_dur, locale)

    transcript = parse_response(payload)
    if not transcript.segments:
        raise TranscriptionError("no speech recognized in audio")
    return transcript
