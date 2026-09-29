> Historical specification: this document includes earlier designs or proposals, not necessarily current behavior. See the [documentation index](README.md) for the current guides.

# PRD — Stage 6 (optional): Visual summaries (see-the-video + inline images)

**Status:** Draft · **Owner:** Pikay · **Depends on:** Stage 1 (video.mp4), Stage 3
(transcript), Stage 4 (summarize), Stage 5 (Notion export)

## 1. Problem / Why

Today the summary is text-only — the model never *sees* the video, only its transcript.
For visual content (slides, whiteboards, diagrams, code on screen, demos) that loses a
lot: a picture of the slide is often clearer than a paragraph describing it. We want the
summary to (a) be informed by what's actually on screen and (b) embed the *right frames*
inline so the reader gets a friendlier, illustrated write-up.

This is explicitly a **heavy, opt-in** capability: it adds frame extraction, vision-model
tokens (many images), and image uploads. It must be **off by default** and enabled by a
flag, never changing the cost profile of the normal text path.

## 2. Goal

`fvs summarize <source> --visual` (and `--visual` on `export`) produces a summary that is
**visually grounded** (the model saw representative frames) and **illustrated** (selected
frames embedded at the relevant points), then exported to Notion with those images hosted
natively.

## 3. Verified facts (probed, not assumed)

- ✅ **`gpt-5.6-sol` is vision-capable.** A real frame from the Agent Memory video was
  sent via the Responses API `input_image` and correctly described ("digital whiteboard …
  handwritten notes about memory and an LLM … Chinese subtitles"). No second deployment
  needed.
- ✅ **`notion-client` exposes `file_uploads`.** Frames can be uploaded and hosted inside
  Notion (no external image host / S3 needed); embedded via `image` blocks referencing the
  uploaded file.
- ✅ **Stage 1 already keeps the full `video.mp4`** (never audio-only) — the raw material
  for frame extraction already exists on disk for every processed video.

## 4. Scope (v1 of the visual feature)

**In:**
- New **Stage 2b — frame extraction**: pull candidate frames from `video.mp4` into
  `frames/` (jpg), each tagged with its timestamp.
- **Visual-aware summarize**: feed a bounded set of frames + the transcript to the vision
  model so the summary reflects on-screen content and marks where an image belongs.
- **Illustrated export**: upload the chosen frames to Notion and embed them inline at the
  right spots (with captions), alongside the existing text blocks.
- **Motion clips (GIFs)**: for action/technique content, the model can request a short
  animated GIF over a span instead of a still (D8) — verified feasible end-to-end.
- Strictly **opt-in** via `--visual`; default path unchanged and zero extra cost.
- Frame artifacts cached like every other stage (re-runnable, `--force` aware).

**Out (deferred to a later iteration):**
- **Derived/synthetic diagrams** (LLM-generated Mermaid/graphviz, image-gen diagrams).
  Real-frame + motion-clip extraction first; synthesized visuals are a separate, heavier
  follow-up.
- Full-motion *analysis* (the model reasons about where motion matters, but does not
  frame-by-frame analyze the movement itself — it sees sampled stills).
- Video scene *understanding* beyond sampled stills (no full-motion analysis, no OCR
  pipeline — the vision model reads text in-frame well enough for v1).
- Face/speaker tracking, slide-boundary ML models.
- Batch/playlist.

## 5. Decisions

### LOCKED (D1–D8)

- **D1 — Frame selection: ✅ (a) scene-change detection.** `ffmpeg
  select='gt(scene,<thr>)'` extracts frames where the picture materially changes (slide
  flips, new diagram/whiteboard state). No LLM in this step. Followed by a **dedup pass +
  runaway guardrails** (see D3) so noisy sources don't explode the frame set. Anchor-
  alignment (old option c) is retained only as a fallback when scene detection yields too
  few frames.

- **D2 — Vision architecture: ✅ (a) see-then-write.** When `--visual` is on, send the
  deduped frame set + transcript to `gpt-5.6-sol` in a single multimodal Responses API
  call. The model writes the summary *and* emits image placeholders (e.g.
  `![](frame@MM:SS)`) at the points where a picture helps. The exporter later resolves
  those placeholders to embedded, uploaded frames. Mechanical "illustrate-after" is kept
  only as a degraded fallback if the model emits no parseable placeholders.

- **D3 — Frame budget: ✅ no fixed cap.** The number of frames scales with the video's
  own visual complexity and length — a fixed ceiling would over-sample simple videos and
  starve dense ones. Instead the **scene-change threshold + a dedup pass** govern the count
  organically: a mostly-static talking-head yields few frames, a slide-heavy lecture yields
  many, which is the desired behavior. Frames are downscaled (long edge ~1024px, jpg) to
  keep per-image token cost low. Two **safety guardrails** (not user-facing caps, just
  runaway protection): (1) drop near-duplicate consecutive frames (perceptual/scene dedup)
  so a shaky camera or animated slide doesn't emit hundreds of near-identical stills;
  (2) if the raw scene count is pathologically high (e.g. > ~200 on a short clip, implying
  a bad threshold or noisy source), auto-raise the threshold and re-select rather than
  sending everything. These protect against pathological inputs without imposing an
  arbitrary limit on normal content.

- **D4 — Which frames get embedded: ✅ (a) model picks.** The model *sees* the full
  deduped frame set but only emits `![](frame@MM:SS)` placeholders for the frames genuinely
  worth showing. Only placeholdered frames are uploaded to Notion and embedded; the rest
  are context-only. This keeps the doc friendly (no contact-sheet of near-identical slides)
  and minimizes uploads, and it's the natural fit with see-then-write — the model is
  already reasoning over the frames, so it decides which illustrate best.

- **D5 — Notion image hosting: ✅ native upload.** Verified `client.file_uploads` exists.
  Per kept frame: create upload → send bytes → embed an `image` block referencing the file
  id. Frames live natively in Notion (self-contained, no external host). Only robust
  option; locked.

- **D6 — Captions: ✅ model-written.** Each embedded image gets a caption = `[MM:SS]` + a
  short model-written description (e.g. "Slide: two memory architectures compared"),
  produced during the see-then-write step. Makes images self-explanatory at low extra cost.

- **D7 — Config surface: ✅ single flag.** `--visual` is the only user-facing knob in v1.
  Scene threshold, downscale size, dedup guardrails (and motion-clip params, D8) are sane
  hardcoded defaults, tunable in code but not exposed as flags. Keeps v1 easy to use;
  expose tunables later only if needed.

- **D8 — Motion clips (GIFs) for technique/action content: ✅ IN, verified feasible.**
  For videos where *motion is the content* (ski carving, tennis swing, dance, demos), a
  frozen still is nearly useless — the movement is the point. So the visual output is not
  limited to stills: the model can also request a short **animated GIF** over a time span.
  - **How it fits see-then-write:** the vision model still only *sees* stills, so it can't
    analyze motion directly — but from the transcript ("watch the follow-through here") plus
    seeing that consecutive frames differ a lot, it emits a **motion placeholder**
    `![](clip@MM:SS-MM:SS)` instead of a still `![](frame@MM:SS)`. We then render that span
    to a GIF and embed it. Model decides *where* motion matters; we render the motion.
  - **Verified live (probed, not assumed):** (1) ffmpeg extracts a downscaled looping GIF
    from a span (`fps=8,scale=480` → ~KBs–low MB); (2) Notion `file_uploads` create→send→
    complete accepts `image/gif`; (3) the uploaded GIF embeds as an `image` block with a
    caption and renders with a live URL. Full round-trip confirmed on the real workspace.
  - **Defaults / guardrails:** GIF spans are short (cap ~4–6 s), downscaled (~480px long
    edge), low fps (~8) to keep size sane; a max span length prevents a runaway multi-minute
    GIF. If a GIF would be too large, fall back to a still at the span's midpoint.
  - **Placeholder grammar:** `![](frame@MM:SS)` = still, `![](clip@MM:SS-MM:SS)` = motion
    GIF. The exporter parses both; unknown/oversized spans degrade to a still.

## 6. Proposed design (D1–D8 locked)

- **New Stage 2b — `stages/frames.py`**: `extract_frames(video_path, out_dir, *, force) ->
  list[Frame]` where `Frame = {path, timestamp}`. ffmpeg scene-detect (D1) + dedup +
  runaway guardrails (D3). Downscaled jpg into `frames/`. Cached.
- **Motion rendering — `stages/frames.py`**: `extract_clip(video_path, start, end, out_path)
  -> Path` renders a short downscaled looping GIF (D8) for a requested span, with size
  guardrails + still fallback.
- **Vision-aware summarize** (extend `stages/summarize.py` or a sibling): when frames are
  supplied, build a multimodal Responses API input (transcript + downscaled frames) so the
  model *sees* the content (D2) and emits still/motion placeholders (D4, D8) inline.
- **Extended Stage 5 — `export_notion.py`**: resolve `![](frame@MM:SS)` /
  `![](clip@MM:SS-MM:SS)` placeholders → extract still or GIF → upload via
  `client.file_uploads` (create→send→complete, D5) → emit `image` blocks with model-written
  captions (D6).
- **Models**: a small `Frame` dataclass; the image-placeholder grammar (D8) is the contract
  between summarize (emits) and export (resolves).
- **Pipeline**: `run_all(..., visual=False)`. When true: run Stage 2b, pass frames into
  summarize, let export embed stills/GIFs. Typed `FrameError(ForgeError)` for extraction
  failures.
- **CLI**: `--visual` on `summarize` (and `export`, to re-illustrate an existing run) — D7.
- **Workspace**: `frames_dir` (and clips reuse it). Summary/notion caches already exist.

## 7. Error handling (consistent with existing stages)

- Fail fast if `--visual` but no `video.mp4` present (e.g. local audio-only input).
- ffmpeg frame extraction failure → typed error, don't fall through to a broken run.
- Vision call failure → typed error; optionally degrade to text-only with a warning
  (decision in D2 fallback).
- Notion upload failure per image → skip that image with a warning rather than failing the
  whole export (best-effort, like page archive).

## 8. Success criteria

- `fvs summarize <url> --visual` produces a Notion page where the summary text reflects
  on-screen content AND relevant frames are embedded inline with `[MM:SS]` captions.
- Default (no `--visual`) path is byte-for-byte unchanged and pays zero visual cost.
- Frame extraction, visual summarize, and image-embedding are unit-tested (ffmpeg + vision
  + notion upload mocked).
- Live-verified on the real Agent Memory bilibili video.
- Frame count stays sensible via D3's dedup + runaway guardrails (no unbounded blow-ups),
  while still scaling naturally with video complexity.

## 9. Risks / notes

- **Cost** is the headline risk — many images × vision tokens. D3's dedup + runaway
  guardrails (and downscaling) keep it bounded without an arbitrary cap.
- Scene detection tuning is content-dependent (0.4 is a starting threshold, not sacred).
- Notion File Upload API has size/type constraints + is newer — verify limits during build.
- Placeholder convention must be robust (model must reliably emit parseable markers, or we
  fall back to illustrate-after).
- Multimodal Responses API payloads are large; watch request size limits with many frames.
