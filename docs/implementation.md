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
- **v1 supports bilibili.com only.**
- Download logic is **hand-written per site** (NOT `yt-dlp`). A reference project will
  be folded in.
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
