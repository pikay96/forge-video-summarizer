# Implementation Spec

Companion to the PRD (`docs/prds/video-summarizer-v1-prd.md`). Captures *how* each
pipeline stage is implemented. Python project. Each stage is an independent function
with a file handoff — individually re-runnable and cacheable.

Pipeline: **download → extract → transcribe → summarize.**

---

## Overview — working directory & artifacts

Each run operates in a per-video working directory; every stage reads the previous
stage's file and writes its own, so any stage is independently re-runnable and cached.

```
output/<sanitized-title>[<video-id>]/
├── <video>.mp4        # Stage 1  (skipped for local-file input)
├── metadata.json      # Stage 1  (rich page metadata; ffprobe-only for local files)
├── audio.mp3          # Stage 2  (16 kHz mono MP3)
├── transcript.json    # Stage 3  (raw Azure Speech response — source of truth)
├── transcript.txt     # Stage 3  (human-readable, inline [MM:SS] markers)
└── summary.md         # Stage 4  (final deliverable)
```

Caching rule (all stages): if the output artifact already exists, skip; `--force`
overrides. This lets you re-run just Stage 4 (prompt tweak) without re-downloading or
re-transcribing.

## CLI commands

| Command      | Input                          | Output                        | Notes |
|--------------|--------------------------------|-------------------------------|-------|
| `summarize`  | bilibili URL **or** local file | `summary.md` (runs all 4)     | End-to-end; the common case. |
| `download`   | bilibili URL                   | `<video>.mp4` + `metadata.json` | Stage 1 only. Skipped for local-file inputs. |
| `extract`    | video file / working dir       | `audio.mp3`                   | Stage 2 only. |
| `transcribe` | `audio.mp3`                    | `transcript.json` + `.txt`    | Stage 3 only. |
| `summarize-transcript` | `transcript.*`       | `summary.md`                  | Stage 4 only (name TBD; distinct from the e2e `summarize`). |

Each per-stage subcommand consumes the previous stage's file output, so stages can be
run, inspected, and re-run in isolation.

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

### Bilibili download mechanics (from reference project)
Distilled from `simple_bili_download.py` (the headless core of a Tauri downloader;
UI/frontend intentionally excluded). Auth is a single **SESSDATA cookie** — nothing
else. Four steps:

1. **Auth / headers** — a `requests.Session` with:
   - Browser-like `User-Agent` (bilibili web API rejects non-browser UAs).
   - `Referer: https://www.bilibili.com/` (the media CDN 403s without it).
   - `SESSDATA` cookie (from a logged-in browser) — the *only* auth token; grants
     access to higher qualities / member content. Public low-res works without it.
     Stored in project-local `.env` (`BILI_SESSDATA`).
2. **bvid → cid** — `GET x/web-interface/view?bvid=<BV...>`. Returns `title` and the
   `pages` list (a video can have multiple parts 分P, each with its own `cid`). Also
   the source of the rich `metadata.json`.
3. **cid → stream URLs (DASH)** — `GET x/player/wbi/playurl?bvid&cid&qn=127&fnval=4048`.
   - `fnval=4048` → **DASH**: separate **video-only** and **audio-only** streams.
   - `qn=127` → highest quality the account allows.
   - **WBI signing NOT required** for `playurl` (only needed for e.g. search).
   - API returns streams sorted best-first; take `dash["video"][0]` / `dash["audio"][0]`.
4. **Download + merge** — HTTP GET each stream (Referer header mandatory), then
   `ffmpeg -i video -i audio -c copy -map 0:v:0 -map 1:a:0 out.mp4 -y` (mux, no re-encode).
   (Reference does single-stream GET; the full app uses ranged parallel chunks — a
   speed optimization, optional for v1.)

**Always download the full video (not audio-only).** Even though DASH exposes the
audio stream separately and a summarizer only needs audio today, v1 **always downloads
and keeps the video file**: visual capability (frame analysis / OCR / scene understanding)
is planned, and it needs the real video. Stage 2 extracts audio from the downloaded
video as normal — no audio-only fast-path.

**BV-id extraction:** accept a raw `BVxxxx` id or a full URL; regex `(BV[0-9A-Za-z]+)`.

### Notes / open items
- Multi-part bilibili videos: reference downloads one part via `--page` (1-based). For
  v1 decide whether to default to part 1 or handle multi-part (defer; likely part-1
  default with a flag).

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

---

## Stage 4 — Summarization

### Goal
Turn the transcript into a teacher-clear written summary — rich enough that a reader
who never saw the video understands the topic AND could discuss/defend it (e.g. field
an interviewer's questions on the material).

### Model & API
- **Azure OpenAI via the OpenAI SDK Responses API** (NOT chat.completions).
  ```python
  from openai import OpenAI
  client = OpenAI(base_url=AZURE_OPENAI_ENDPOINT, api_key=AZURE_OPENAI_API_KEY)
  #   base_url e.g. https://<res>.services.ai.azure.com/openai/v1
  resp = client.responses.create(model=DEPLOYMENT, input=<prompt>)
  ```
- Deployment (v1): `gpt-5.6-sol`. Endpoint / key / deployment from project-local `.env`.

### Summary language
- **Match the video's dominant language** (option c). The transcript carries a detected
  locale (Stage 3); the summary is written in that dominant language. Mixed EN/CN video
  → summary in whichever dominates.

### Structure (teacher-clear + interview-ready)
Markdown `summary.md`, length scaling with duration:
1. **Title** — the video title.
2. **TL;DR** — 2-3 sentences: what the video is and its single core takeaway.
3. **Context / why it matters** — brief framing: what problem/topic, who'd care.
4. **Walkthrough (chapters)** — sectioned by meaningful topic shift, each with a
   `[MM:SS]` / `[HH:MM:SS]` anchor. Each section *explains* the idea like a teacher
   (not "he says X" — actually convey the concept, with the reasoning), scaling depth
   with the video length.
5. **Key takeaways** — bulleted, what the reader should walk away knowing.
6. **Q&A / interview prep** — the questions the material answers, each with a concise
   answer, so a reader could be questioned on the content and hold their own. Include
   the kind of probing/"why/how" questions an interviewer would ask.

### Anchors
- **One `[MM:SS]` anchor per meaningful topic shift** (not fixed count). Naturally
  scales — short video → few, long lecture → many. Model guided by "anchor each real
  topic transition," anchors reference the original video timeline (from Stage 3
  segment offsets).

### Long-transcript handling (v1)
- **v1 assumes the transcript fits the model's context window** (option a) — consistent
  with the no-chunk philosophy. No map-reduce in v1.
- If a transcript is too large to fit, **fail with a clear error** rather than silently
  dropping content. Map-reduce (summarize sections → summarize summaries) is a
  documented future extension, mirroring the Stage-3 chunking extension point.

### Output (the handoff)
- `output/<title>[id]/summary.md` — the final deliverable.

### Caching
- If `summary.md` exists, skip (cache hit); `--force` re-summarizes. Re-running Stage 4
  alone (e.g. to tweak the prompt) reuses the cached transcript.
