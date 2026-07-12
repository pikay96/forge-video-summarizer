# Video Summarizer - Product Requirements Document (PRD)

## Requirements Description

### Background
- **Business Problem**: Watching a full video to extract its knowledge is slow. A
  reader wants to understand a video's content — clearly and completely — without
  watching it end to end.
- **Target Users**: Primary user is Pikay (personal tool). Feeds it mostly YouTube
  links, occasionally other sites or local video files.
- **Value Proposition**: Turn any video into a teacher-quality written summary — a
  reader who has never seen the video comes away genuinely understanding the topic,
  with timestamped anchors to jump back into the source when they want detail.

### Feature Overview
- **Core Features**: A four-stage pipeline, each stage an independent function with
  a file handoff, so any stage is individually re-runnable and its output cacheable:
  1. **Download** — fetch video (or accept a local file), extract audio.
  2. **Extract audio** — normalize to the format the transcriber needs.
  3. **Transcribe** — speech-to-text with timestamps (Azure OpenAI Whisper).
  4. **Summarize** — LLM turns the transcript into a teacher-clear summary.
- **Feature Boundaries**:
  - IN: single video in → single summary out; YouTube + other sites + local files
    (one code path); auto-chunking of long audio; timestamped anchors in output.
  - OUT (v1): playlists / channel batches; live streams; authenticated or private
    videos; multi-video jobs; any GUI.
- **User Scenarios**:
  - Run the whole pipeline with one command: input → `summary.md`.
  - Run any single stage standalone (e.g. re-summarize an existing transcript with a
    different prompt, without re-downloading or re-transcribing).

### Detailed Requirements
- **Input/Output**:
  - Input: a URL (YouTube or other supported site) **or** a path to a local video file.
  - Intermediate artifacts (each a file handoff): downloaded audio → normalized audio
    → transcript (text + timestamps) → summary.
  - Final output: `summary.md` — a written summary whose length scales with video
    duration, containing inline timestamped anchors in `[MM:SS]` / `[HH:MM:SS]` format.
- **User Interaction** (dual interface):
  - **End-to-end command**: one command runs all four stages input → summary.
  - **Per-stage commands**: four standalone subcommands — `download`, `extract`,
    `transcribe`, `summarize` — each consuming the previous stage's file output.
- **Data Requirements**:
  - Input classification: value is treated as a URL if it looks like one, otherwise
    as a local file path (local files skip the download stage).
  - Transcript carries per-segment timestamps referenced to the **original video's**
    timeline (see Edge Cases — chunk offsetting).
  - Configuration (Azure endpoint, credentials, deployment names, tunable params)
    lives in a **project-local `.env`**, never committed.
- **Edge Cases**:
  - **Long audio > Whisper 25 MB request limit**: audio is auto-chunked into
    size-safe segments; each segment is transcribed independently; transcripts are
    stitched back into one.
  - **Timestamp integrity across chunks** (correctness constraint): each chunk's
    Whisper timestamps restart at 0, so before stitching, every chunk's timestamps
    MUST be offset by that chunk's start time in the original audio. Summary anchors
    must point at true positions in the source video, not chunk-relative positions.
  - **Local file input**: download stage is skipped; pipeline enters at audio extraction.
  - **Unsupported / unreachable source**: fail clearly with an actionable message
    rather than producing a partial or empty summary.

## Design Decisions

### Technical Approach
- **Architecture Choice**: Staged pipeline with file handoffs. Rationale — each stage
  is independently re-runnable, debuggable, and cacheable; a failure or a prompt tweak
  only re-runs the affected stage, not the whole chain.
- **Key Components**:
  - Download/extract: `yt-dlp` (handles YouTube + ~1800 sites; local files fall through).
  - Audio normalization: `ffmpeg`, resampled to 16 kHz mono for the transcriber.
  - Transcription: **Azure OpenAI Whisper** deployment (with client-side chunking).
  - Summarization: **Azure OpenAI** chat deployment.
  - CLI: single entrypoint exposing an e2e command plus four per-stage subcommands.
- **Data Storage**: Local filesystem only. Per-run working directory holds the
  intermediate artifacts and final `summary.md`. No database.
- **Interface Design**: Subcommand CLI. Config resolved from project-local `.env`.
  (Exact env-var names and deployment wiring are implementation detail, out of PRD scope.)

### Constraints
- **Performance Requirements**: No hard latency target for v1 (batch/offline tool).
  Must handle long videos correctly via chunking rather than failing.
- **Compatibility**: Runs locally (WSL/Linux). Depends on `yt-dlp` and `ffmpeg` on PATH.
- **Security**: Azure credentials only in project-local `.env`, git-ignored. Transcript
  and audio are sent to Azure OpenAI (accepted trade-off for quality; documented).
- **Scalability**: v1 is single-video, single-run. Batch/playlist/live/auth deferred.

### Risk Assessment
- **Technical Risks**:
  - Chunk-boundary timestamp drift → mitigated by explicit per-chunk offsetting.
  - Chunking mid-word/mid-sentence degrades transcript quality → prefer splitting on
    silence or with small overlap where practical.
- **Dependency Risks**:
  - `yt-dlp` breakage when sites change → pin a version, allow easy upgrade.
  - Azure OpenAI quota / rate limits on long chunked jobs → surface errors clearly.
- **Schedule Risks**: Chunking + timestamp-offset logic is the main complexity spike;
  everything else is thin wrappers.

## Acceptance Criteria

### Functional Acceptance
- [ ] E2E command turns a YouTube URL into a `summary.md` in one invocation.
- [ ] Each of the four stages (`download`, `extract`, `transcribe`, `summarize`) runs
      standalone against the previous stage's file output.
- [ ] Local video file input is accepted and correctly skips the download stage.
- [ ] Audio exceeding the Whisper 25 MB limit is auto-chunked, transcribed, and stitched
      into a single coherent transcript.
- [ ] Summary anchors in `[MM:SS]`/`[HH:MM:SS]` point to correct positions in the
      original video (verified on a video long enough to force chunking).
- [ ] Summary length scales with video duration and reads as teacher-clear: a reader
      who never saw the video understands the topic.

### Quality Standards
- [ ] Code Quality: each stage a clean, independently callable function.
- [ ] Test Coverage: timestamp-offset logic across chunk boundaries has explicit tests.
- [ ] Config hygiene: no secrets committed; `.env` git-ignored; `.env.example` provided.

### User Acceptance
- [ ] User Experience: one command for the common case; per-stage control when needed.
- [ ] Documentation: README documents both the e2e command and per-stage subcommands.

## Execution Phases

### Phase 1: Preparation
**Goal**: Environment and config scaffolding
- [ ] Confirm `yt-dlp` + `ffmpeg` available; pin `yt-dlp` version.
- [ ] Define project-local `.env` schema + `.env.example`; wire Azure client config.
- **Deliverables**: runnable skeleton + config loading
- **Time**: ~0.5 day

### Phase 2: Core Development
**Goal**: Implement the four stages + both interfaces
- [ ] Stage 1 download/extract (yt-dlp; local-file passthrough).
- [ ] Stage 2 audio normalization (ffmpeg → 16 kHz mono).
- [ ] Stage 3 transcription with chunking + per-chunk timestamp offset + stitch.
- [ ] Stage 4 summarization (teacher-clear prompt, duration-scaled, timestamped anchors).
- [ ] CLI: e2e command + four per-stage subcommands.
- **Deliverables**: working pipeline end to end
- **Time**: ~2 days

### Phase 3: Integration & Testing
**Goal**: Correctness on real inputs
- [ ] Test on a short video (no chunking) and a long video (forces chunking).
- [ ] Verify anchor positions against the source timeline.
- [ ] Verify each stage runs standalone from cached artifacts.
- **Deliverables**: verified pipeline + tests for timestamp offsetting
- **Time**: ~0.5 day

### Phase 4: Deployment
**Goal**: Usable + documented
- [ ] README: setup, `.env`, e2e + per-stage usage.
- [ ] Tag v1.
- **Deliverables**: documented v1
- **Time**: ~0.25 day

---

**Document Version**: 1.0
**Created**: 2026-07-12
**Clarification Rounds**: 3
**Quality Score**: 92/100
