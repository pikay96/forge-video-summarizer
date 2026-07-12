# Implementation Spec

Companion to the PRD (`docs/prds/video-summarizer-v1-prd.md`). Captures *how* each
pipeline stage is implemented. Python project. Each stage is an independent function
with a file handoff — individually re-runnable and cacheable.

Pipeline: **download → extract → transcribe → summarize.**

---

## Stage 1 — Download

### Goal
Given a video URL, download the video file and write a rich metadata sidecar into a
per-video working directory named after the (sanitized) video title.

### Source support (v1)
- **v1 supports bilibili.com URLs, and local video files.**
  - A **local video file path** as input skips the download stage entirely and enters
    the pipeline at Stage 2 (audio extraction). No metadata sidecar is fetched for
    local files (only what ffprobe can read).
- Remote download logic is **hand-written per site** (NOT `yt-dlp`). A reference
  project will be folded in.
- **Layered / pluggable design** — a downloader abstraction (base interface +
  per-site implementation) so bilibili is implementation #1 and other sites (YouTube,
  etc.) slot in behind the same interface without touching the pipeline.

  ```
  Downloader (interface)
    ├─ can_handle(url) -> bool
    ├─ fetch_metadata(url) -> Metadata
    └─ download(url, dest_dir) -> video_path, metadata
         └─ BilibiliDownloader   (v1)
         └─ (YouTubeDownloader)  (future)
  ```
  A small registry/factory picks the downloader by URL.

### Input
- A video URL (v1: a bilibili.com URL).

### Output (the handoff)
- The **downloaded video file** (full video, not audio-only — audio extraction is a
  separate stage).
- A **metadata sidecar** file (JSON) alongside the video.

### Working directory
- Layout: `output/<sanitized-title>[<video-id>]/`
  - Directory name is the **sanitized video title** (human-readable).
  - A **short video-id suffix** guards against collisions when two different videos
    sanitize to the same title, and keeps the dir unique. e.g.
    `Some_Video_Title_[BV1xx411c7XD]/`
- Contains the video file + `metadata.json` (+ later stages' artifacts).

### Filename sanitization
- Sanitize the title with **native string handling** (NOT an LLM):
  - Replace/strip filesystem-invalid characters: `/ \ : * ? " < > |`, plus control chars.
  - Trim trailing dots/spaces; collapse whitespace; cap length to a filesystem-safe
    bound (e.g. ~150 chars).
- The **raw, unmodified title** is preserved in `metadata.json`.

### Metadata sidecar (`metadata.json`)
Capture **as much as the site page/API exposes** — useful fields feed the summary,
the rest is free provenance. Target fields (grab what's available):
- `video_id` (e.g. bilibili BV id)
- `title` (raw, unsanitized)
- `source_url`
- `duration`
- `uploader` / author (name + id)
- `upload_date` / publish time
- `description`
- `tags` / categories
- `cover_image_url` (thumbnail)
- Engagement stats where shown: view / like / coin / favorite / share counts
- `parts` / page list (for multi-part videos)
- `resolution` / available formats
- `download_timestamp` (when we fetched it)

### Notes / open items
- Reference download project (to be shared) will inform the concrete bilibili fetch
  (API endpoints, signing, stream selection).
- Multi-part bilibili videos: decide whether v1 downloads a single part or all parts
  (defer until reference project is reviewed).

---

## Transcription API limits (verified)

**Transcriber = Azure Speech Services *fast transcription*** (`speechtotext/
transcriptions:transcribe`, e.g. `mai-transcribe-1.5`) — NOT Azure OpenAI Whisper.

Verified limits (Microsoft Learn, "Quotas and limits for Azure Speech", Standard S0,
retrieved 2026-07-12):
- **Maximum audio input file size: < 500 MB**
- **Maximum audio length: < 5 hours per file**
- Maximum requests per minute: 600

Implication for chunking (Stage 3): the 25 MB figure was the *OpenAI Whisper* limit
and does NOT apply here. With a 500 MB / 5-hour ceiling, most videos fit in a single
request — chunking is only needed for very long content (multi-hour). The chunking +
timestamp-offset machinery still gets built (for the >5h / >500MB tail and safety
margin), but it will rarely trigger. Threshold to be set in the Stage 3 spec.
