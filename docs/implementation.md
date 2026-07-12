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

---

## Stage 2 — Audio extraction

### Goal
Take the video (downloaded in Stage 1, or a local file passed directly) and produce
the audio file the transcriber ingests.

### Input
- The video file from Stage 1's working dir, **or** a local video file path (which
  enters the pipeline here, skipping Stage 1).

### Output (the handoff)
- `output/<title>[id]/audio.mp3` — **16 kHz mono MP3.**
  - Chosen over WAV: Azure Speech fast transcription accepts compressed audio; MP3 is
    ~7 MB/hour vs WAV's ~115 MB/hour, so a video would need to be ~70 h long to hit
    the 500 MB API limit. Chunking (Stage 3) therefore effectively never fires.
  - 16 kHz mono is sufficient for speech; no meaningful transcription-quality loss.

### Extraction
- Plain `ffmpeg` extraction — downmix to mono, resample to 16 kHz, encode MP3:
  ```
  ffmpeg -i <video> -vn -ac 1 -ar 16000 -c:a libmp3lame -q:a 4 <out>/audio.mp3
  ```
- **No loudness normalization or silence trimming** (deliberate):
  - Azure Speech handles varied input levels fine.
  - Silence trimming would shift timestamps and break the anchor-integrity guarantee
    (Stage 3). Keep the audio timeline identical to the source.
  - If transcription quality ever disappoints, normalization can be added later as an
    opt-in — it's not needed for v1.

### Caching
- If `audio.mp3` already exists in the working dir, **skip re-extraction** (cache hit).
  A `--force`/overwrite flag can override later if needed.

### Notes
- For a local-file input with no Stage 1 metadata, `ffprobe` provides duration and
  basic stream info used downstream.

---

## Stage 3 — Transcription

### Goal
Turn `audio.mp3` into a timestamped transcript, via Azure Speech Services fast
transcription, preserving the original language.

### API
- **Azure Speech Services fast transcription** — `POST {endpoint}/speechtotext/
  transcriptions:transcribe?api-version=2025-10-15`.
- multipart/form-data: `audio=@audio.mp3` + a `definition` JSON.
- Auth via `Ocp-Apim-Subscription-Key` (endpoint + key from project-local `.env`).

### `definition` config (v1)
- **`enhancedMode`: enabled**, model `mai-transcribe-1.5`, `transcribeStyle: verbatim`
  (better quality).
- **`phraseList.phrases`: empty but wired** — the field is present and pluggable so
  domain terms can be injected per-run later; ships empty in v1.
- **Language: auto-detect** (language identification). Bilibili content is frequently
  mixed English/Chinese, so a fixed locale is wrong. Provide candidate locales
  (e.g. `zh-CN`, `en-US`) for LID rather than pinning one.
- **Transcript stays in the ORIGINAL language** — no translation at this stage.
  (Translation, if ever wanted, is a Stage-4 summary concern, not transcription.)

### Timestamp granularity
- **Segment / phrase level** (offset + duration per recognized phrase) — sufficient
  for `[MM:SS]` topic anchors. Word-level not needed for v1.

### Output (the handoff) — both artifacts
- `output/<title>[id]/transcript.json` — **raw Azure response** (source of truth:
  phrases, offsets, durations, confidence, detected locale).
- `output/<title>[id]/transcript.txt` — **derived human-readable** transcript with
  inline `[MM:SS]` / `[HH:MM:SS]` markers per segment.
- Stage 4 may consume either; keeping both fits the explicit file-handoff design and
  lets the transcript be eyeballed.

### Chunking (v1: OFF, extension point retained)
- **v1 does NOT chunk.** The 500 MB / 5 h fast-transcription ceiling comfortably covers
  realistic inputs (16 kHz mono MP3 ≈ 7 MB/h → ~70 h to reach 500 MB; the 5 h limit
  binds first).
- **Guard, not silent truncation**: before calling the API, check duration/size against
  the limits (e.g. > ~5 h or > ~500 MB). If exceeded, **fail with a clear, actionable
  error** telling the user the input is too long for v1 — never truncate silently.
- **Extensibility**: the chunk → per-chunk-timestamp-offset → stitch design is kept as
  a documented future path (single place to implement). When added, each chunk's
  segment timestamps MUST be offset by the chunk's start time before stitching so
  anchors stay true to the original timeline.

### Caching
- If `transcript.json` already exists, skip re-transcription (cache hit); `--force`
  overrides.
