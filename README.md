# forge-video-summarizer

Local-capable video-to-summary pipeline. Five independent stages, each a clean
function with a file handoff so any stage is re-runnable and cacheable.

```
video URL / local file
   │  per-site downloader (bilibili / Xiaohongshu)         → video.mp4 + metadata.json
   ▼
audio.wav (16 kHz mono)  │  ffmpeg
   │
   ▼  transcribe model  (Azure Speech fast transcription, MAI enhanced — best zh/en text)
transcript.json + transcript.txt (segment timestamps)
   │
   ▼  LLM summarize      (Azure OpenAI Responses API)
summary.md   (teacher-clear + interview-ready, [MM:SS] anchors)
   │
   ▼  export             (Notion SDK)
Notion subpage   (embedded video + metadata + summary, clickable timestamp links)
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

**Optional — overview image renderer.** Each summary gets an auto-generated **Excalidraw
overview image** (see [Overview image](#overview-image)). Rendering needs a Node CLI plus a
headless browser (one-time):

```bash
npm install                              # installs excalidraw-brute-export-cli
npx playwright install chromium firefox  # headless browsers it drives
```

If the renderer is absent the pipeline still runs — it just skips the overview image.

## Configure

Copy `.env.example` to `.env` and set:

- `BILI_SESSDATA` — SESSDATA cookie from a logged-in bilibili session (only auth needed).
- `XHS_COOKIE` — optional Cookie for Xiaohongshu; usually blank (video pages are public).
- `AZURE_SPEECH_ENDPOINT` / `AZURE_SPEECH_KEY` — Azure Speech (fast transcription).
  `AZURE_SPEECH_MODEL` (optional) sets the enhanced model, default `mai-transcribe-1.5`.
- `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` — summarization.
- `NOTION_API_KEY` / `NOTION_PARENT_PAGE_ID` — Notion export (Stage 5). The integration
  must be connected to the parent page (page ••• → Connections). `NOTION_PARENT_PAGE_ID`
  accepts a bare id, dashed UUID, or a full Notion page URL.

## Usage

End-to-end (the common case):

```bash
fvs summarize https://www.bilibili.com/video/BV1xxxxxxx
fvs summarize /path/to/local/video.mp4
fvs summarize <url> --export        # also publish to Notion (Stage 5)
```

Per-stage (each consumes the previous stage's file output):

```bash
fvs download   <bilibili-url>      # Stage 1 → video.mp4 + metadata.json
fvs extract    <url|local-file>    # Stage 2 → audio.wav
fvs transcribe <url|local-file>    # Stage 3 → transcript.json + .txt
fvs summarize-transcript <url|local-file>   # Stage 4 → summary.md
fvs export     <url|local-file>    # Stage 5 → Notion subpage (needs summary.md)
```

Flags: `--output <dir>` (default `output/`), `--force` (ignore cache), `--env <path>`.

## Stages

1. **Download** — hand-written per-site downloaders behind a pluggable interface,
   picked by URL:
   - **bilibili** — SESSDATA auth, `view` → `playurl` DASH → ffmpeg mux.
   - **Xiaohongshu / RedNote (小红书)** — resolve `xhslink.com` short links, parse the
     note page's `__INITIAL_STATE__`, pull the single progressive MP4
     (`originVideoKey` → CDN, or the highest-res stream). No login required; an optional
     `XHS_COOKIE` helps if you hit an anti-bot wall. **Video posts only.**

   Always keeps the full video (planned visual capability needs it). Rich `metadata.json`
   sidecar. Adding a site = one class implementing `Downloader` + a line in the registry.
2. **Extract audio** — `ffmpeg` → 16 kHz mono PCM WAV (the format the fast-transcription
   endpoint accepts directly). No normalization/trimming (keeps the timeline identical so
   anchors stay accurate).
3. **Transcribe** — Azure Speech **fast transcription** with **enhancedMode (MAI)** for
   best mixed zh/en text quality (recovers inline English terms + punctuation). ~9s for
   a 27-min video. MAI returns one block, so per-segment `[MM:SS]` anchors are
   interpolated (approximate — transcript.txt says so); transcript.json keeps the raw
   response as source of truth.
4. **Summarize** — Azure OpenAI Responses API. Teacher-clear + interview-ready markdown,
   length scales with duration, one `[MM:SS]` anchor per meaningful topic shift. Then a
   **dedicated second pass** authors an **Excalidraw overview image** from the finished
   summary and renders it to `overview.png` (see below).
   - **Optional `--slides`** (for slide/PPT-style talks): scene-detect distinct slides,
     pick the frame whose caption hides the least, and let the vision model choose the KEY
     slides and place each next to the point it illustrates (as `![slide@MM:SS]`).
     Real slide screenshots embedded inline — kept *in addition to* the overview image.
     **Frames are embedded exactly as captured — never cropped, never masked.** On-screen
     captions are handled by picking a different moment, not by editing pixels: cropping
     and masking were tried and removed because they destroyed real slide content.
     Best-effort; never blocks the summary.
5. **Export** — publishes `summary.md` to Notion as a **subpage** of a configured parent
   page (official `notion-client` SDK). Each page carries the **overview image** (uploaded
   via Notion `file_uploads`) at the top, an embedded bilibili video, a metadata callout,
   and the summary as native blocks; `[MM:SS]` anchors are plain text (bilibili's web
   player ignores `?t=` deep links). Idempotent (dedups by video-id marker in the title,
   archives + recreates) and chunks block appends at Notion's 100-per-request limit. Audio
   and transcript are intentionally excluded.

## Overview image

Every summary gets an auto-generated **Excalidraw overview image** — a clean, hand-drawn
concept map of the video's key ideas and how they relate, embedded at the top of the Notion
page. It's produced in a **dedicated second model pass** (after the prose summary): the model
authors an Excalidraw scene (structured JSON — real labels, correct relationships, in the
summary's dominant language), which is rendered to `overview.png` and uploaded to Notion.

Excalidraw is used deliberately over Mermaid (whose auto-layout came out cramped/hard to
read) and over image generation (which invents garbled labels on technical content): the
model authors a **structured** scene, so labels are faithful while the layout is clean and
readable. Rendering uses `excalidraw-brute-export-cli` (headless browser); if it's absent the
step degrades gracefully and the summary ships without the image.

## Layout

```
src/forge_video_summarizer/
├── cli.py            # subcommand CLI
├── pipeline.py       # orchestration + caching/file-handoff
├── config.py         # .env loading
├── workspace.py      # per-video dir, title sanitize, artifact paths
├── models.py         # VideoMetadata, Transcript, TranscriptSegment
├── errors.py         # typed exceptions
├── downloaders/      # pluggable per-site (base + bilibili + xiaohongshu + registry)
├── prompts/          # vendored excalidraw specialist instruction (package data)
└── stages/           # extract, transcribe, summarize, diagram, export_notion
tests/                # unit tests (network/subprocess/Notion API/renderer mocked)
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
- Stage 5 PRD: [`docs/PRD-stage5-notion-export.md`](docs/PRD-stage5-notion-export.md)
- Implementation spec: [`docs/implementation.md`](docs/implementation.md)

## Scope (v1)

Single video in → single summary out. No batch/playlist, no live streams, no
authenticated/private videos. Chunking (long audio) and map-reduce (long transcript)
are documented extension points, not built in v1.
