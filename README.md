# forge-video-summarizer

Local-capable video-to-summary pipeline. Four independent stages, each a clean
function with a file handoff so any stage is re-runnable and cacheable.

```
video URL / local file
   │  hand-written per-site downloader (v1: bilibili)   → video.mp4 + metadata.json
   ▼
audio.wav (16 kHz mono)  │  ffmpeg
   │
   ▼  transcribe model  (Azure Speech fast transcription, MAI enhanced — best zh/en text)
transcript.json + transcript.txt (segment timestamps)
   │
   ▼  LLM summarize      (Azure OpenAI Responses API)
summary.md   (teacher-clear + interview-ready, [MM:SS] anchors)
```

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill in credentials
```

Requires `ffmpeg` (and `ffprobe`) on PATH. On WSL/Linux without root, a static build
works: download from https://johnvansickle.com/ffmpeg/, then drop `ffmpeg`/`ffprobe`
into `~/.local/bin`. (A Windows `ffmpeg.exe` reached via `/mnt/c` PATH can raise
`PermissionError` under WSL — prefer a native Linux binary.)

## Configure

Copy `.env.example` to `.env` and set:

- `BILI_SESSDATA` — SESSDATA cookie from a logged-in bilibili session (only auth needed).
- `AZURE_SPEECH_ENDPOINT` / `AZURE_SPEECH_KEY` — Azure Speech (fast transcription).
  `AZURE_SPEECH_MODEL` (optional) sets the enhanced model, default `mai-transcribe-1.5`.
- `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` — summarization.

## Usage

End-to-end (the common case):

```bash
fvs summarize https://www.bilibili.com/video/BV1xxxxxxx
fvs summarize /path/to/local/video.mp4
```

Per-stage (each consumes the previous stage's file output):

```bash
fvs download   <bilibili-url>      # Stage 1 → video.mp4 + metadata.json
fvs extract    <url|local-file>    # Stage 2 → audio.wav
fvs transcribe <url|local-file>    # Stage 3 → transcript.json + .txt
fvs summarize-transcript <url|local-file>   # Stage 4 → summary.md
```

Flags: `--output <dir>` (default `output/`), `--force` (ignore cache), `--env <path>`.

## Stages

1. **Download** — hand-written per-site downloader behind a pluggable interface
   (v1 = bilibili: SESSDATA auth, `view` → `playurl` DASH → ffmpeg mux). Always keeps
   the full video (planned visual capability needs it). Rich `metadata.json` sidecar.
2. **Extract audio** — `ffmpeg` → 16 kHz mono PCM WAV (exactly what the Speech SDK
   consumes, so Stage 3 needs no re-transcode). No normalization/trimming (keeps the
   timeline identical so anchors stay accurate).
3. **Transcribe** — Azure Speech **fast transcription** with **enhancedMode (MAI)** for
   best mixed zh/en text quality (recovers inline English terms + punctuation). ~9s for
   a 27-min video. MAI returns one block, so per-segment `[MM:SS]` anchors are
   interpolated (approximate — transcript.txt says so); transcript.json keeps the raw
   response as source of truth.
4. **Summarize** — Azure OpenAI Responses API. Teacher-clear + interview-ready markdown,
   length scales with duration, one `[MM:SS]` anchor per meaningful topic shift.

## Layout

```
src/forge_video_summarizer/
├── cli.py            # subcommand CLI
├── pipeline.py       # orchestration + caching/file-handoff
├── config.py         # .env loading
├── workspace.py      # per-video dir, title sanitize, artifact paths
├── models.py         # VideoMetadata, Transcript, TranscriptSegment
├── errors.py         # typed exceptions
├── downloaders/      # pluggable per-site (base + bilibili + registry)
└── stages/           # extract, transcribe, summarize
tests/                # 87 unit tests, ~97% coverage (network/subprocess mocked)
```

## Test

```bash
pytest --cov=forge_video_summarizer --cov-report=term-missing
ruff check src tests
```

## Debugging (VS Code F5)

`.vscode/` is committed. Open the folder and press **F5** — configurations are provided
for: run current file, run the CLI (`summarize` with a URL/path prompt), pytest current
file, and pytest all. `.vscode/.env` sets `PYTHONPATH=src` so imports resolve before an
editable install. Also runnable as a module: `python -m forge_video_summarizer …`.

## Docs

- PRD: [`docs/prds/video-summarizer-v1-prd.md`](docs/prds/video-summarizer-v1-prd.md)
- Implementation spec: [`docs/implementation.md`](docs/implementation.md)

## Scope (v1)

Single video in → single summary out. No batch/playlist, no live streams, no
authenticated/private videos. Chunking (long audio) and map-reduce (long transcript)
are documented extension points, not built in v1.
