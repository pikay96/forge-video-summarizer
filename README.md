# forge-video-summarizer

Local-capable video-to-summary pipeline. Four independent stages, each a clean
function with a file handoff so any stage is re-runnable and cacheable.

```
video URL/file
   │  yt-dlp            (thin wrapper: URL → download, local file → skip)
   ▼
audio.wav (16kHz mono) │  ffmpeg
   │
   ▼  transcribe model  (Azure OpenAI)
transcript.txt (+ timestamps)
   │
   ▼  LLM summarize      (Azure OpenAI)
summary.md
```

## Stages

1. **Download** — `yt-dlp`. YouTube + ~1800 sites; local files fall through to
   stage 2. No playlist/live/auth handling in v1.
2. **Extract audio** — `ffmpeg`, resampled to 16 kHz mono for the transcribe model.
3. **Transcribe** — a transcribe model (Azure OpenAI) → text + timestamps. Long
   audio is auto-chunked past the API request-size limit, then stitched back with
   per-chunk timestamp offsets so anchors stay true to the original timeline.
4. **Summarize** — an LLM (Azure OpenAI) turns the transcript into a teacher-clear
   summary: length scales with duration, inline `[MM:SS]`/`[HH:MM:SS]` anchors.

## Interface

- **End-to-end**: one command runs all four stages, input → `summary.md`.
- **Per-stage**: four standalone subcommands — `download`, `extract`, `transcribe`,
  `summarize` — each consuming the previous stage's file output.

## Config

Azure endpoint, credentials, and deployment names live in a project-local `.env`
(git-ignored; see `.env.example`).

## Scope (v1)

Single video in → single summary out. No batch/playlist, no live streams, no
authenticated/private videos.

## Docs

- PRD: [`docs/prds/video-summarizer-v1-prd.md`](docs/prds/video-summarizer-v1-prd.md)

## Status

Scaffold + PRD done — implementation in progress.
