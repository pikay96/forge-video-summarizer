# Engineering notes

`forge-video-summarizer` is a single-video Python CLI pipeline, not an agent platform. Its useful boundary is an inspectable workspace: media, transcript, prose, and optional images can be examined separately before publishing. These notes describe the checked-in implementation; the older PRDs record design discussions, not a complete statement of current behavior.

## Pipeline boundaries

[`Pipeline.run_all`](../src/forge_video_summarizer/pipeline.py#L191) sequences the work synchronously:

| Boundary | Implementation | Handoff |
| --- | --- | --- |
| Acquire media | [`Downloader`](../src/forge_video_summarizer/downloaders/base.py#L22), selected by the [registry](../src/forge_video_summarizer/downloaders/__init__.py#L26) | Full video and `metadata.json`; registered sites are Bilibili, Xiaohongshu, and Douyin. |
| Extract audio | [`extract_audio`](../src/forge_video_summarizer/stages/extract.py#L20) | `audio.wav`: 16 kHz mono PCM, without silence trimming or loudness normalization. |
| Transcribe | [`transcribe_audio`](../src/forge_video_summarizer/stages/transcribe.py#L240) | `transcript.json` and readable `transcript.txt`. |
| Write notes | [`summarize_transcript`](../src/forge_video_summarizer/stages/summarize.py#L129) | `summary.md`, with a separate overview-image pass and optional screenshot selection. |
| Publish, when requested | [`export_summary`](../src/forge_video_summarizer/stages/export_notion.py#L386) | A Notion child page; its URL is saved in `notion_url.txt`. |

A [local-file input](../src/forge_video_summarizer/pipeline.py#L63) is copied into the workspace instead of downloaded. Keeping the full video supports later screenshot extraction; PCM simplifies the transcription input and preserves the audio timeline, at the cost of disk space. Preserving that timeline does **not** make the generated sentence timestamps exact.

Functions and file handoffs keep failures inspectable without a database or job service. The CLI exposes stage commands, but those commands still take the original video source, not arbitrary transcript or workspace paths. [`cli._workspace_for`](../src/forge_video_summarizer/cli.py#L60) resolves that source before dispatching.

## Cache: inexpensive reuse, not reproducible builds

[`Workspace.should_skip`](../src/forge_video_summarizer/workspace.py#L99) checks only whether a path exists and `force` is false. This avoids repeated transcription and summary calls during iteration. It does not hash inputs, validate completeness, record prompt/model versions, or invalidate dependent outputs.

Consequences worth knowing:

- Changing the model, prompt, language, or `--slides` option does not invalidate an existing `summary.md`. The [summary cache check](../src/forge_video_summarizer/pipeline.py#L123) also skips both image steps.
- A changed transcript does not automatically refresh the summary; a changed summary does not refresh a cached Notion URL. Existing images are not automatically removed when regeneration fails or slides are disabled.
- [`run_download`](../src/forge_video_summarizer/pipeline.py#L48) fetches metadata **before** checking for cached media. URL-based stage commands can therefore need network access even when the expensive artifacts exist.
- Workspaces use title plus video ID. A remote title change can select a different directory. Local files use the stem plus the literal ID `local`, so equal stems can collide; an existing local copy is not replaced, even by an end-to-end forced run.
- A partial artifact can look like a cache hit. A cached Notion URL is not a live check that the page still exists.

[`--force` is a global CLI option](../src/forge_video_summarizer/cli.py#L16), placed before the subcommand, for example `fvs --force export <source>`. On URL-based stage commands it also reaches workspace acquisition, so it can trigger a download rather than only the named stage. Calling a stage method with an existing `Workspace` offers narrower control, but that is a Python API workflow, not a dedicated resume-by-workspace CLI.

## Transcription: text quality versus timestamp precision

The request uses Azure Speech fast transcription with enhanced mode and verbatim text. The [implementation rationale](../src/forge_video_summarizer/stages/transcribe.py#L1) and history describe a preference for mixed Chinese/English text quality over precise phrase timing; this is not a general accuracy benchmark.

For a single returned phrase, [`parse_response`](../src/forge_video_summarizer/stages/transcribe.py#L74) splits sentences and distributes duration in proportion to character count. Pauses and changing speech rates can produce substantial drift. Multi-phrase responses retain their supplied offsets. [`Transcript.to_timestamped_text`](../src/forge_video_summarizer/models.py#L85) labels interpolated anchors as approximate; they are navigation hints, not subtitle alignment. The summary prompt receives that warning, but the code does not enforce that the final prose repeats it.

### What long-audio support actually does

The [implemented limits and chunk calculation](../src/forge_video_summarizer/stages/transcribe.py#L138) use `300 * 1024 * 1024` bytes and 7,200 seconds per request, with a 0.9 sizing margin. These are code constants, not a claim about every current Azure service tier. Chunking can start before the hard limits. If duration is unavailable, the estimate assumes the pipeline's PCM format at 32,000 bytes per second.

[`_split_audio`](../src/forge_video_summarizer/stages/transcribe.py#L157) creates equal-duration temporary WAV slices with ffmpeg stream copy. Requests run sequentially with a 600-second request timeout and a per-request byte guard. There is no silence-aware split, overlap, per-chunk checkpoint, or application-level retry loop. A failed later request does not leave earlier chunks available for a resumed run.

One subtle provenance distinction: a single-request `transcript.json` preserves the returned API payload. For chunked input, [`_stitch_payloads`](../src/forge_video_summarizer/stages/transcribe.py#L182) constructs a merged payload containing concatenated text and one phrase spanning the full duration. The individual raw responses are not persisted. Sentence anchors are then interpolated across the **whole video**, not offset from retained chunk boundaries. Calling every cached transcript an untouched raw response would be inaccurate.

Audio chunking does not solve summary context limits. [`summarize_transcript`](../src/forge_video_summarizer/stages/summarize.py#L129) sends the full timestamped transcript in one text request. There is no token-budget preflight or map-reduce summarization.

## Overview image: a separate, best-effort pass

[`generate_overview_image`](../src/forge_video_summarizer/stages/diagram.py#L218) reads the finished summary and asks for structured Excalidraw JSON. [`render_excalidraw`](../src/forge_video_summarizer/stages/diagram.py#L147) writes the scene beside `overview.png` and invokes a Node exporter backed by a headless browser visiting excalidraw.com.

The separate pass isolates diagram formatting from prose generation and permits one regeneration after invalid JSON or a failed render. Missing renderer tooling skips the image. The pipeline catches diagram failures and continues with the summary.

The cost is another model request and a browser/network dependency. Validation checks parseable structure and a successful, nonempty render—not factual relationships, label readability, or overlap. The PNG is a separate artifact, placed first in the Notion export; it is not inserted into the local Markdown automatically.

## Optional screenshots: selection, not general video understanding

`--slides` is designed for slide-style talks. [`detect_slide_candidates`](../src/forge_video_summarizer/stages/frames.py#L239) combines scene cuts, nearby-cut clustering, and sampling across long gaps. It then searches nearby frames for less caption obstruction. The caption heuristic targets bright-yellow pixels near the bottom, so it is not general subtitle detection.

[`select_slide_placements`](../src/forge_video_summarizer/stages/slides.py#L229) sends batches of six candidates to a vision request. Images are normally downscaled to an 800-pixel maximum width and JPEG quality 70 for selection; export uses the original captured PNGs. The model returns slide/anchor pairs, and [`place_slides`](../src/forge_video_summarizer/stages/slides.py#L462) inserts placeholders without rewriting the prose. Failed batches can be skipped, and selecting no screenshots is valid.

Important limitations in the actual data flow:

- The selector receives images, labels, caption scores, and **timestamp anchors only**—not the summary's prose or section meanings. It cannot reliably establish semantic placement from that input alone.
- Deterministic replacement prefers a lower-penalty candidate within 20 seconds, but does not verify that it depicts the same slide. Quality filtering uses relative caption and edge-activity scores, not a content-completeness test.
- [`_pick_clean_frame`](../src/forge_video_summarizer/stages/frames.py#L205) may choose a later sample while retaining the window's original timestamp label. That label is not necessarily the exact capture time.
- Placement is restricted to a recognized Walkthrough section; [`_walkthrough_slice`](../src/forge_video_summarizer/stages/slides.py#L190) falls back to the whole document when no matching heading is found.

Captured images are not cropped or masked for export. Sampling can still miss the useful frame, and a correct frame can appear beside the wrong topic. The pipeline does not perform motion analysis, comprehensive OCR, source-name correction, or visual fact checking of the prose. Manual image and placement review remains necessary.

## Notion: archive and recreate, not an atomic upsert

The exporter converts Markdown into native blocks, uploads available images, and creates a child page under the configured parent. Initial children and subsequent appends are limited to 100 blocks per request. Audio and transcript files are not uploaded by this stage; timestamps remain plain text.

[`_find_existing`](../src/forge_video_summarizer/stages/export_notion.py#L322) matches the **entire title including its video-ID suffix**, not the ID alone. A match is archived before a new page is created. This is simpler than clearing every old block, but changes the page ID and URL and does not preserve edits made on the old page.

It is not transactional: lookup and archive errors are suppressed; duplicates can result. A creation failure can leave the old page archived, and an append failure can leave a partial new page. There is no built-in readback verification, rollback, or concurrent-writer protection. Review the resulting page rather than treating a returned URL as proof of a complete export.

## Privacy and operational boundaries

Local execution is not local-only processing. Audio goes to Azure Speech; transcript and metadata context go to Azure OpenAI for prose; the finished summary goes into the diagram request; candidate screenshots go into vision requests. Notion export sends the summary, source metadata, and available images. Diagram rendering also requires the external Excalidraw web application.

[`load_config`](../src/forge_video_summarizer/config.py#L76) reads environment variables and `.env`, with process values taking precedence. Credentials, cookies, source URLs, local paths, transcripts, and screenshots should be treated as sensitive. There is no application-level redaction or retention policy. In particular, the exporter forwards `metadata.source_url`; it does not sanitize local paths or token-bearing URLs for publication.

Platform login and anti-bot failures remain operational constraints. The [Xiaohongshu downloader](../src/forge_video_summarizer/downloaders/xiaohongshu.py#L145) accepts an optional cookie but has no interactive login/recovery flow. Browser sign-in, extracting authenticated page state, or obtaining platform subtitles through external tools are manual/assistant-assisted actions, not shipped `fvs` features. A Notion integration also needs user-granted access to the parent page.

## Modest next steps — proposed, not built

1. Add artifact manifests and atomic file writes, with source/config fingerprints and explicit downstream invalidation. A workspace-based CLI would avoid unnecessary metadata requests.
2. Preserve each chunk's raw response and timing provenance; checkpoint successful chunks. Add a summary context-size check before paying for a request.
3. Pass section prose to screenshot selection, retain the actual capture timestamp, and review placements against a small, varied set of recordings.
4. Make Notion replacement failures explicit, verify new page contents before retiring the old page, and record export state for recovery. This would still need careful handling to provide stable URLs.
5. Add explicit timeout/retry policy for text requests and a small manual evaluation checklist. The [current text wrapper](../src/forge_video_summarizer/stages/_openai.py#L39) relies on SDK defaults rather than a project-defined policy.

These are targeted reliability improvements to a personal CLI, not a plan for a distributed processing platform. See [AI-assisted development workflow](../workflow.md) for how specs, tests, and real-output review fit together.
