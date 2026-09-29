# Forge Video Summarizer

**Turn videos into illustrated notes—with clear explanations, selected screenshots, and concept maps.**

A personal Python application for turning talks, tutorials, and other videos into material you can revisit. Instead of returning only a short recap, it organizes ideas by topic, explains the key concepts, and adds takeaways and review questions. Optional screenshots retain useful visual context; Notion export brings the result into a notebook.

**Stack:** Python · FFmpeg · Azure Speech · Azure OpenAI · Excalidraw · Notion

[Run locally](#run-locally) · [Engineering notes](docs/engineering.md) · [Development guide](docs/development.md) · [Verification](docs/verification/README.md)

## What you get

- **Topic-based notes:** an overview, key takeaways, explanations organized by concept rather than chronology, and review questions.
- **A concept map:** a separate model pass creates an Excalidraw overview image when the optional renderer is installed.
- **Selected screenshots:** `--slides` selects frames from slide-style recordings to illustrate the notes. Selection and placement need review.
- **Language control:** follow the transcript's dominant language, or explicitly request English or Chinese.
- **Optional Notion export:** native text blocks, available images, source metadata, and a video embed.

Generated names and claims still need checking. This is a learning aid, not exact subtitles or automatic fact checking.

### Inspectable output

```text
output/<title>[<video-id>]/
  video.mp4            Full source video; extension may vary
  metadata.json        Source details
  audio.wav            Extracted audio
  transcript.json      Transcription payload; merged for chunked input
  transcript.txt       Readable text with approximate sentence anchors
  summary.md           Structured notes
  overview.excalidraw  Optional concept-map scene
  overview.png         Optional rendered concept map
  slides/              Optional captured frames
  notion_url.txt       Created after Notion export
```

This is an artifact map, not a captured demo. A curated, redistributable example is not yet included; use your own local video to inspect the workflow. The [verification record](docs/verification/README.md) separates software checks from live-service and content-quality evaluation.

## How it works

```text
Local video / supported URL
          |
          v
Acquire video -> Extract audio -> Transcribe -> Write notes
                                                  |
                                  +---------------+---------------+
                                  |                               |
                           Overview image                  Selected frames
                           (optional tooling)              (--slides)
                                  |                               |
                                  +---------------+---------------+
                                                  |
                                        Notion export (--export)
```

Five stages communicate through files in a per-video workspace. The process runs synchronously on one machine; transcription and generation use cloud services. Intermediate artifacts let you inspect failures and reuse completed work without a database or job service.

**Inputs:** local video files, Bilibili, Xiaohongshu / RedNote, and Douyin video posts. Platform changes, login requirements, and network restrictions can interrupt URL downloads. Local files are the most predictable starting point. YouTube URLs, playlists, image galleries, and live streams are not supported.

## Engineering decisions

- **Preserve intermediate artifacts.** Files make failures and generated results inspectable. The tradeoff is disk usage and existence-based caching rather than automatic dependency invalidation.
- **Favor readable multilingual transcription.** Enhanced transcription is used for text quality; sentence timing may be interpolated. Anchors are navigation hints, not precise evidence alignment.
- **Separate prose and diagram generation.** A second request focuses on the concept map. Rendering validates the scene, not the factual correctness of its relationships.
- **Use original frames.** A vision model selects sampled screenshots; exported images are not cropped or masked. Sampling and placement can still miss the right content.
- **Keep visuals best-effort.** A renderer or screenshot-selection failure can degrade to text notes rather than fail the whole task.

The [engineering notes](docs/engineering.md) explain these decisions, their failure modes, and proposed improvements with links to implementation. The [AI-assisted workflow](workflow.md) distinguishes development assistance and manual review from shipped application features.

## Run locally

Requires Python **3.10+**, `ffmpeg`, `ffprobe`, Azure Speech credentials, and an Azure OpenAI deployment supporting the Responses API. The recorded validation uses Python 3.11. Cloud calls may incur charges.

```bash
git clone https://github.com/pikay96/forge-video-summarizer.git
cd forge-video-summarizer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

The clone requires access while the repository is private. On Windows PowerShell use `.venv\Scripts\Activate.ps1`. Fill in the Azure Speech and Azure OpenAI fields in `.env`; see the [configuration guide](docs/development.md#configuration) for endpoint shapes and optional services.

Replace the example path with a video you own or are authorized to process:

```bash
# Write English notes for a local video.
fvs summarize /path/to/talk.mp4 --language en

# For a different, slide-based recording, also select screenshots.
fvs summarize /path/to/another-talk.mp4 --language en --slides
```

For overview images, install the optional renderer **before** generating a new summary:

```bash
npm ci
npx playwright install chromium firefox
```

Inspect `summary.md`, the transcript, and the images before publishing. The overview is a separate file; screenshot placeholders become images during Notion export. Local Markdown is not a standalone image gallery.

After configuring a Notion integration and connecting it to the parent page:

```bash
fvs export /path/to/talk.mp4
```

**Two important behaviors:**

- Changing language, model, or `--slides` does not invalidate existing output. See [stage-level reuse](docs/development.md#stage-level-reuse); `--force` goes before the subcommand and may repeat paid work.
- Re-export can archive the matching Notion page and create another, changing the URL and not preserving edits on the old page. It is not an atomic update. Verify the result before sharing.

## Validation and limitations

```bash
python -m pytest --cov=forge_video_summarizer --cov-report=term-missing
ruff check src tests
```

The [recorded local run](docs/verification/README.md) passed **254 tests**, reported **90% statement coverage**, and passed Ruff. These are commit-scoped software checks, not summary-accuracy scores or a live-service benchmark.

The application currently handles one video per run. Long audio is split for transcription, but the full transcript still goes into one summarization request. There is no batch scheduler, hosted UI, conversational agent, or retrieval index. Timestamp drift, missed frames, and incorrect screenshot placement remain possible. Interactive browser login and case-specific corrections are manual operations, not CLI features.

## Privacy and attribution

Local execution is not local-only processing: audio goes to Azure Speech; text and optional screenshots go to Azure OpenAI; Notion export sends notes, metadata, and images. Overview rendering loads the external Excalidraw application. No application-level redaction or automatic retention policy is implemented.

Do not commit credentials, cookies, private recordings, or generated personal notes. Review exported metadata for local paths and signed URLs before sharing. Process only authorized media and follow source-platform terms.

Downloader work draws on [bilibili-video-downloader](https://github.com/lanyeeee/bilibili-video-downloader), [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader), and [douyin-downloader](https://github.com/jiji262/douyin-downloader). The [Douyin signer header](src/forge_video_summarizer/downloaders/_abogus.py) identifies vendored code. Attribution does not replace a distribution-license review; there is currently no top-level license file.

## Further reading

- [Engineering notes](docs/engineering.md): decisions, tradeoffs, and source links.
- [Development guide](docs/development.md): configuration, stage commands, and troubleshooting.
- [Verification](docs/verification/README.md): recorded checks and an output-review checklist.
- [AI-assisted workflow](workflow.md): specifications, tests, and real-input review.
- [Documentation index](docs/README.md): current guides and historical specifications.
