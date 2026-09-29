# Development guide

[Project overview](../README.md) · [Engineering notes](engineering.md) · [Verification](verification/README.md)

## Installation

The CLI runs on the local machine but calls cloud services for transcription and generation. Use Python 3.10 or later; Python 3.11 is used in the recorded validation.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
fvs --help
```

On Windows PowerShell use `python -m venv .venv` and `.venv\Scripts\Activate.ps1`. Ensure native `ffmpeg` and `ffprobe` executables are on `PATH`. Within WSL, prefer Linux binaries instead of accidentally invoking Windows executables through `/mnt/c`.

The Python dependencies use minimum versions rather than a lock file; a new installation can resolve different versions. The [verification record](verification/README.md) lists the environment actually tested, not a guarantee for every future dependency release.

### Optional overview renderer

```bash
npm ci
npx playwright install chromium firefox
```

The Node manifest pins the Excalidraw export CLI. It uses a headless browser and network access to excalidraw.com. Install the browser system dependencies required by Playwright for your operating system. If rendering is unavailable, the pipeline can still write text notes. Installing it later does not invalidate a cached summary automatically.

## Configuration

Configuration comes from `.env` or the file selected by `--env`. Process environment values take precedence. Never paste credentials into issues, screenshots, or committed examples.

### Minimum for local-video notes

```dotenv
AZURE_SPEECH_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
AZURE_SPEECH_KEY=<your-speech-key>
AZURE_SPEECH_MODEL=mai-transcribe-1.5
AZURE_OPENAI_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1
AZURE_OPENAI_API_KEY=<your-openai-key>
AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>
```

These values are placeholders. Use your resource's actual endpoints and deployment name. Azure Speech uses fast transcription with enhanced mode; the configured model must be available on your resource. Summarization uses the OpenAI SDK's Responses API. Screenshot selection additionally needs an image-capable deployment. Credentials are not necessarily interchangeable between separate Azure resources.

The default output language follows the transcript's dominant language. `--language en` or `--language zh` explicitly sets the summary language; it does not translate the stored transcript.

### Optional sources

- `BILI_SESSDATA`: a Bilibili session cookie, which can affect accessible media quality.
- `XHS_COOKIE`: optional Xiaohongshu cookie. Public access is not guaranteed; authentication, platform changes, or anti-bot controls may still prevent extraction.
- `DOUYIN_COOKIE`: optional cookie. The adapter otherwise attempts anonymous cookie bootstrapping and signed detail requests.

These settings are for authorized access. The CLI does not manage interactive browser login, capture cookies for you, or guarantee recovery from site restrictions. A local video avoids dependency on website extraction, but cloud stages still require their services.

### Optional Notion export

```dotenv
NOTION_API_KEY=<your-integration-secret>
NOTION_PARENT_PAGE_ID=<parent-page-id-or-url>
```

Connect the integration to the parent page in Notion before exporting. The parent may be supplied as a bare ID, dashed UUID, or a Notion page URL. Notes are created as child pages, not database records.

Source metadata is forwarded to Notion. A local source path or a signed URL may be visible in the resulting page; review and sanitize the metadata before publication where appropriate.

## Commands

Run from the same directory, with the same output root, when reusing a workspace:

```bash
# The local file must exist; replace this path.
fvs summarize /path/to/talk.mp4 --language en

# All global flags precede the subcommand.
fvs --env /path/to/settings.env --output /path/to/notes summarize /path/to/talk.mp4

# Generate and immediately export after Notion configuration.
fvs summarize /path/to/talk.mp4 --export

# For an existing reviewed summary, export separately.
fvs export /path/to/talk.mp4
```

Per-stage commands take the **original source**, not the previous artifact's path:

```bash
fvs download <supported-video-url>
fvs extract <video-url-or-local-file>
fvs transcribe <video-url-or-local-file>
fvs summarize-transcript <video-url-or-local-file> --language en
fvs export <video-url-or-local-file>
```

For example, `summarize-transcript` expects the matching workspace to contain `transcript.json`; it does not accept an arbitrary `.txt` transcript. For URL inputs, stage commands resolve/download through the source adapter first, so they can refetch metadata even with cached media.

## Stage-level reuse

Caches are based on file existence. Changing a prompt, model, language, or screenshot option does not invalidate downstream artifacts. A changed source with the same local filename stem may reuse an old copied video. Partial artifacts can also be mistaken for completed work.

`fvs --force summarize <source>` forces the pipeline stages, not just the summary. `fvs --force export <source>` can also repeat URL acquisition and archive/recreate the matching Notion page. There is no transactional rollback. Old image files may survive failed regeneration or a run without screenshots.

For narrower control, operate on an existing workspace through Python. This example regenerates notes from its cached transcript without calling the source adapter; it can incur model charges and does not publish anything:

```python
from pathlib import Path

from forge_video_summarizer.config import load_config
from forge_video_summarizer.pipeline import Pipeline
from forge_video_summarizer.workspace import Workspace

# Use the exact title and video_id from your workspace's metadata.json.
root = Path("output")
workspace = Workspace(root, "Exact title", "exact-video-id")
pipeline = Pipeline(load_config(), root)
pipeline.run_summarize(workspace, force=True, language="en", slides=True)
```

Review the regenerated Markdown and visual files before calling `pipeline.run_export(workspace, force=True)`. An export force operation replaces a matching page by archiving and recreating it, not by surgically updating individual blocks. Preserve any manual notes separately first.

## Troubleshooting

- **Download fails or returns a login page:** verify access in the source platform. Use an authorized local copy if available. Interactive browser recovery is outside the CLI.
- **No overview image:** check the Node dependencies, installed Playwright browsers, network access, and whether summary caching skipped generation. Image generation is best-effort.
- **No screenshots:** the selector can legitimately keep none. Check whether the recording contains diagrams or spatial information worth illustrating; do not weaken filtering merely to fill the page.
- **Wrong image placement:** compare the source video with the nearby paragraph. Approximate transcript anchors and selector limitations can misalign them. Manual corrections are not automatic application behavior.
- **Language did not change:** an existing `summary.md` was likely reused. Use the stage API for scoped regeneration rather than retranscribing unnecessarily.
- **Long recording fails at summarization:** audio chunking does not limit the final transcript request size. There is no map-reduce fallback.
- **Notion returns 404:** confirm the integration can access the configured parent. A token alone does not grant page access.
- **Notion link exists but the page is incomplete:** read back the page and all child blocks. A partial append can leave a partial page, and a cached URL is not a completion check.

## Tests and debugging

```bash
python -m pytest --cov=forge_video_summarizer --cov-report=term-missing
ruff check src tests
python -m forge_video_summarizer --help
```

The tests mock service and many subprocess boundaries. Successful tests do not establish current site availability or generated content quality. Follow the separate [manual review checklist](verification/README.md#manual-output-review).

The committed `.vscode/` configuration provides F5 launch entries for the CLI, current file, and pytest. Select the virtual environment as the Python interpreter. `.vscode/.env` sets `PYTHONPATH=src`; it is not the service-credentials file.

## Code map

```text
src/forge_video_summarizer/
  cli.py          Argument parsing and dispatch
  pipeline.py     Stage orchestration and artifact reuse
  config.py       Environment configuration
  workspace.py    Per-video paths
  models.py       Metadata and transcription data structures
  errors.py       Application exception types
  downloaders/    Bilibili, Xiaohongshu, Douyin, registry and interface
  stages/         Audio, transcription, notes, diagrams, frames, and export
  prompts/        Packaged Excalidraw instructions
 tests/           Contract and regression tests
```

The detailed [engineering notes](engineering.md) link to specific implementation boundaries and explain why a simple local pipeline was chosen over a service architecture.
