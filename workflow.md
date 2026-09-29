# AI-assisted development workflow

`forge-video-summarizer` is an AI-assisted personal project. AI assistance is part of the development and investigation process, not a multi-agent architecture inside `fvs`. Specifications, executable tests, and inspection of real outputs provide the checks around generated changes.

The repository supports a concrete loop: **define the boundary, implement and test it, exercise a real input, inspect the result, then revise**. Git history records examples of that loop, not proof that every change followed strict test-first development.

## 1. Start with a small specification

The [original PRD](docs/prds/video-summarizer-v1-prd.md) and [implementation notes](docs/implementation.md) establish single-video scope and file handoffs. In history, `5899b0d` adds the PRD and `b1bcb9d` begins the stage specification before `0010016` implements the initial pipeline.

Those documents also contain superseded details. Use the [engineering notes](docs/engineering.md) and linked source for current behavior; a proposed feature in a PRD is not evidence that it shipped.

## 2. Make behavior testable

The implementation exposes stage functions and injectable clients. Tests use fixtures and mocked service boundaries to check contracts rather than requiring paid API calls:

- [Pipeline tests](tests/test_pipeline.py): artifact handoffs, cache reuse, and optional-step degradation.
- [Transcription tests](tests/stages/test_transcribe.py): approximate anchors, response parsing, request errors, and chunk orchestration.
- [Screenshot tests](tests/stages/test_slides.py): label validation, placement, batching, and failure handling.

These tests can catch regressions in plumbing. They do not establish transcript accuracy, summary completeness, or visual relevance. Their presence also does not establish whether a human or assistant authored an individual implementation or test.

## 3. Use real inputs and review the artifacts

Commit `4aceb89` records a real Douyin share-link failure that led to session isolation and regression tests. Commit `493c469` records a missing screenshot investigation: the frame existed, but selection changed when it was shown alone, motivating smaller batches. Commit `5cc5ea6` records user feedback that cropping and masking removed useful information; those operations were removed.

These are historical development reports, not a fresh benchmark. A useful manual review checks names and key claims against the source, inspects each screenshot and its placement, and opens the exported page to check rendering—not just whether a file or URL exists.

## 4. Keep assisted operations separate from shipped features

A script or external assistant can call stage functions against cached files, inspect frames, help recover authenticated media, or edit a particular summary. Those actions do not become CLI capabilities automatically. Case-specific travel-note corrections, including a manually reviewed Seoul summary, must not be presented as automatic name verification, subtitle ingestion, or screenshot alignment in `fvs`.

The shipped boundary is the [CLI](src/forge_video_summarizer/cli.py), [pipeline](src/forge_video_summarizer/pipeline.py), and their stage implementations. A reproducible improvement needs to live there with appropriate tests; a one-off corrected output is evidence of review, not evidence of a generalized feature.
