# forge-video-summarizer

Local-capable video-to-summary pipeline. Four independent stages, each a clean
function with a file handoff so any stage is re-runnable and cacheable.

```
video URL/file
   │  yt-dlp            (thin wrapper: URL → download+extract, local file → skip)
   ▼
audio.wav (16kHz mono) │  ffmpeg
   │
   ▼  faster-whisper
transcript.txt / .srt (+ timestamps)
   │
   ▼  LLM summarize
summary.md
```

## Stages

1. **Download** — `yt-dlp -x --audio-format wav`. YouTube + ~1800 sites; local
   files fall through to stage 2. No playlist/live/auth handling in v1.
2. **Extract audio** — `ffmpeg`, resampled to 16kHz mono for Whisper.
3. **Transcribe** — `faster-whisper` (CTranslate2, CPU/GPU). Text + timestamps.
4. **Summarize** — feed transcript to an LLM (external API or local model — TBD).

## Scope (v1)

Single video in → single summary out. No batch/playlist, no live streams, no
authenticated/private videos.

## Status

Scaffold only — implementation in progress.
