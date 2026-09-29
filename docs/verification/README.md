# Verification record

[Project overview](../../README.md) · [Engineering notes](../engineering.md)

## Recorded local checks

Checked on **2026-09-29 UTC**, against application and test code at commit [`ecf8cc586a9c11aded4285b04b1a0eb85c7f9593`](https://github.com/pikay96/forge-video-summarizer/commit/ecf8cc586a9c11aded4285b04b1a0eb85c7f9593). The accompanying presentation changes do not modify application code or tests.

Environment: Ubuntu under WSL2, Python 3.11.15, a fresh virtual environment installed with `python -m pip install -e ".[dev]"`. No service credentials were copied into that checkout.

Selected resolved versions: OpenAI SDK 3.20.0, notion-client 3.1.0, Pillow 12.3.0, pytest 9.1.1, pytest-cov 7.1.0, Ruff 0.16.9. These identify the tested environment; the project does not currently lock Python dependencies.

| Check | Observed result | What it establishes |
| --- | --- | --- |
| Editable install in a new virtual environment | Succeeded | Python packaging and dependency installation on this environment. |
| `python -m pip check` | `No broken requirements found.` | Declared dependency compatibility, not live API compatibility. |
| `python -m pytest --cov=forge_video_summarizer --cov-report=term-missing` | **254 passed, 71 warnings in 13.60s** | Contract and regression tests, mostly with mocked external boundaries. |
| Statement coverage | **90%**, 2,123 statements and 203 missed | Coverage of the measured suite, including vendored signer code; not an accuracy score. |
| `ruff check src tests` | `All checks passed!` | Configured lint rules pass. |
| `fvs --help` and `fvs summarize --help` | Exit 0 | Installed CLI entry point and documented flags are available. |
| Local media extraction smoke test | Valid mono, 16 kHz `pcm_s16le` WAV | Real ffmpeg execution through the installed CLI and local workspace path. |

The 71 warnings are Pillow `Image.getdata()` deprecation warnings from frame/scoring tests. They did not fail the suite, but indicate future compatibility work.

### Extraction smoke test

The input was an explicitly synthetic one-second blue frame with a 440 Hz tone, generated solely to exercise media extraction. It contains no spoken material and is **not** a summary-quality demo. Temporary paths below are local test artifacts, not repository examples.

```bash
ffmpeg -v error -y \
  -f lavfi -i color=c=blue:s=320x240:d=1 \
  -f lavfi -i sine=frequency=440:duration=1 \
  -c:v mpeg4 -c:a aac -shortest /tmp/fvs-doc-smoke.mp4

fvs --output /tmp/fvs-doc-smoke-output extract /tmp/fvs-doc-smoke.mp4

ffprobe -v error \
  -show_entries stream=codec_name,sample_rate,channels \
  -show_entries format=duration -of json \
  '/tmp/fvs-doc-smoke-output/fvs-doc-smoke[local]/audio.wav'
```

Observed WAV fields: codec `pcm_s16le`, sample rate `16000`, channels `1`, duration `1.021688` seconds. The duration is reported as observed, not rounded to the nominal synthetic-input length.

## What this record does not establish

This documentation pass did **not** run live Azure transcription or generation, a fresh website download, the Excalidraw browser renderer, or Notion export. It does not provide a latency/cost benchmark, a user-adoption metric, a calibrated quality score, or a claim of production readiness. No hosted CI run is implied by local test results.

Historical real-input investigations appear in the [development workflow](../../workflow.md). They are reports in project history, not substitutes for a reproducible current benchmark. A curated demo and a small, reviewed evaluation set remain useful next additions.

## Manual output review

For each recording, keep a source reference, processing configuration, and notes about any manual edits. Do not silently present an edited output as raw generation.

1. **Content coverage:** compare the notes against the source. Check important concepts, caveats, examples, and screen-only material; do not mistake fluent prose for completeness.
2. **Names and claims:** verify unfamiliar terms against the source audio and visible text. A second speech-recognition result is corroboration, not proof. Keep uncertain words explicit.
3. **Timing:** distinguish approximate sentence anchors from measured frame or subtitle timestamps. Check that an image actually illustrates the adjacent paragraph.
4. **Visuals:** inspect the concept map for wrong relationships, unreadable labels, and overlap. Inspect every selected screenshot for clipped or hidden source information; preserve original pixels rather than masking away content.
5. **Publication:** inspect metadata for secrets or private paths, then verify the Notion page and paginate through all blocks. Check images, formatting, completeness, and whether replacement changed the URL.
6. **Evidence:** record input duration, model/deployment, enabled options, wall time by stage, and corrections. Keep configuration and measurement context with any performance claim.

Use varied material—such as English narration, mixed-language speech, and a slide-heavy recording—rather than calibrating every decision against one example. Choose recordings that can legally be shared before adding public samples.
