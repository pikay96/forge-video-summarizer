# PRD — Overview diagram (Mermaid) in the summary

> **SUPERSEDED 2026-07:** the overview was switched from Mermaid to an **Excalidraw image**.
> Pikay found the rendered Mermaid "ugly and hard to read" and asked for a cleaner visual —
> "it doesn't have to be a diagram, just an image that can clearly show the topic overview."
> As-built: the model authors an Excalidraw scene (structured JSON, faithful labels, dominant
> language) → rendered to `overview.png` via `excalidraw-brute-export-cli` (headless browser)
> → uploaded to Notion via `file_uploads` and embedded as an image block at the TOP of the
> page (no longer a text block inside summary.md). The core reasoning below still holds
> (structured-authoring beats image-gen for faithfulness; dedicated second pass; always-on;
> dominant-language labels; graceful degrade). What changed: renderer (mmdc → excalidraw CLI),
> placement (in-summary text block → uploaded image at page top), and validation (strict mmdc
> parse → render-succeeds check). See the `forge-video-summarizer` skill for as-built detail.

**Status:** Locked (all decisions D + O1–O5 settled; ready to build) · **Owner:** Pikay · **Depends on:** Stage 4 (summarize), Stage 5
(Notion export) · **Relation:** enhances the *original* text summarizer; independent of the
Stage 6 visual-frames feature (that PRD covers real frames/GIFs; this covers a structured
concept diagram).

## 1. Problem / Why

The summary is well-structured prose, but a reader landing on a 27-minute technical video's
write-up has no **at-a-glance map** of how the concepts fit together. A single overview
diagram — "here's the shape of the whole thing" — makes the summary dramatically friendlier
to skim and to remember.

## 2. Goal

Every summary gets **one overview diagram** at the top: a **Mermaid** diagram that captures
the video's structure/key concepts and their relationships, rendered natively in Notion.

## 3. The core decision — Mermaid, not image-gen (LOCKED)

**Decision: model-generated Mermaid code, NOT an image-generation model.** Locked after
explicitly weighing "final quality, ignoring cost."

- An **image-gen model** optimizes for *plausible appearance*, not *faithfulness*. On
  technical content it renders **invented labels, garbled text, and made-up structure** —
  actively misleading for a study aid. More budget does not fix this; image models
  approximate how something *looks*, they don't represent what it *is*.
- **Mermaid** is generated from the model's actual *understanding*: structured code with
  **real concepts, correct labels, accurate relationships**. For an overview, "quality" =
  faithful + legible, which is a structural-representation problem — Mermaid's home turf.
- Image-gen only wins when the *visual appearance itself* is the content (what something
  looks like). That need is already served by the separate Stage 6 real-frame/GIF PRD.
  This feature is the complementary "how it's structured" axis.
- **Excalidraw rejected:** programmatic generation means emitting its JSON scene schema
  (fiddly) and it doesn't render natively in Notion. Worse fit than Mermaid.

**Verified live (probed, not assumed):** Notion accepts a `code` block with
`language: "mermaid"` and renders it as a real diagram (code/preview toggle). Confirmed via
API by creating a mermaid mind-map of the Agent Memory structure, reading it back, and
cleaning up the probe page.

## 4. Second decision — dedicated second pass (LOCKED)

**Decision: generate the diagram in a dedicated second model call**, after the summary is
written — NOT inline in the summarize call. Locked under the quality-first framing.

Why second pass is higher quality:
1. **Undivided attention** — the model isn't splitting focus between writing prose and
   designing a diagram (prose otherwise wins and the diagram is an afterthought).
2. **Better raw material** — it diagrams the *finished, distilled summary*, not the raw
   transcript mid-thought, yielding cleaner structure.
3. **The specialist instruction is fully applied** — the mermaid guidance (type-selection
   matrix, node shapes, best practices, <20-node rule) becomes the *entire* system prompt
   for this call instead of being diluted against summarization instructions. Biggest
   quality lever; only works well in a separate pass.
4. **Isolated validation + retry** — a malformed diagram can be regenerated without
   touching the good summary.

Accepted trade-offs (fine under cost-no-object): one extra API round-trip; prose↔diagram
coherence recovered by feeding the finished summary into the diagram call.

## 5. Third decision — diagram type is model's choice (LOCKED)

**Decision: the model picks the diagram type per content**, guided by the specialist's
decision matrix — not a fixed type. Process/algorithm → flowchart; interactions → sequence;
data model → ERD; architecture → C4; lifecycle/states → state diagram; concept-heavy →
mind-map/flowchart. A technical lecture usually lands on flowchart or mind-map; forcing a
single type would misrepresent some videos.

## 6. The instruction (LOCKED source)

The diagram call's system prompt is the **mermaid-diagram-specialist** skill, vendored into
the package at `src/forge_video_summarizer/prompts/mermaid-diagram-specialist.md` (shipped
as `package-data`, loaded via `importlib.resources` — not a live URL that can drift). It
provides: the type-selection matrix, per-type syntax + node shapes, styling, and best
practices (keep < ~20 nodes, subgraphs for grouping, clear labels, test rendering).

## 7. Scope

**In:**
- A dedicated diagram-generation step: summary.md → one Mermaid diagram.
- Model chooses diagram type via the specialist matrix.
- Diagram prepended to the summary (an "## Overview" section with a ```mermaid block).
- Export renders it natively: map `mermaid` through the code-block language table so Notion
  shows a diagram (currently unknown languages flatten to "plain text").
- Cached/idempotent like every stage; re-runnable with `--force`.

**Out (v1):**
- Multiple diagrams per summary (one overview only).
- Diagram styling/theming/brand colors (default Mermaid theme).
- Rendering Mermaid → static image ourselves (Notion renders it; no headless renderer).
- Human-in-the-loop diagram editing round-trips.

## 8. Decisions O1–O5 (LOCKED)

- **O1 — Always-on: ✅.** The overview diagram is part of the *default* summarize output,
  not behind a flag. It's cheap (one text call) and strictly improves the summary.
- **O2 — Placement: ✅ top.** Prepended as an `## Overview` section before the prose — a map
  you read before diving in.
- **O3 — Validation: ✅ strict (real Mermaid parse).** Before embedding, validate the
  generated Mermaid by actually rendering it with `@mermaid-js/mermaid-cli` (`mmdc`). A
  clean render (exit 0 + output produced) = valid; a parse/unknown-diagram error (exit 1,
  no output) = invalid → regenerate (O4). Chosen over light structural checks so we catch
  *every* syntax failure, not just obvious ones.
  - **Verified live (probed, not assumed):** on this machine — Node v22 + npm present;
    `@mermaid-js/mermaid-cli` installs and its headless Chromium runs on WSL; a valid
    diagram renders (exit 0, PNG produced); an invalid one returns exit 1 with
    "Parse error on line N" and no output; an unknown diagram type returns exit 1 with
    "UnknownDiagramError". So exit-code + output-existence is a reliable validity signal.
  - **New dependency:** `@mermaid-js/mermaid-cli` (Node, via npm) — a *build/runtime
    tool*, not a Python package. Document in README setup; detect at runtime and, if absent,
    degrade to embedding the unvalidated block (still renders in Notion when correct).
- **O4 — Retry on failure: ✅.** If validation fails, regenerate once — feed the invalid
  Mermaid + the parser's error message back to the model and ask it to fix. If the *retry*
  also fails validation, degrade gracefully: ship the summary and embed the raw block as a
  code listing (readable, just not a rendered picture) + warn. Never block the summary.
- **O5 — Label language: ✅ match dominant language.** Diagram labels use the summary's
  dominant language (Chinese for the current content) so the diagram reads consistently with
  the prose. Steered explicitly in the diagram instruction.

## 9. Proposed design (O1–O5 locked)

- **New step — `stages/diagram.py`**:
  - `generate_overview_diagram(summary_markdown, config, *, client=None) -> str` returns a
    validated Mermaid code block. Loads the specialist prompt via
    `importlib.resources.files("forge_video_summarizer.prompts")`, sends it as
    `instructions`, passes the finished summary as input, steers labels to the summary's
    dominant language (O5), requests exactly one fenced ```mermaid block.
  - `validate_mermaid(code) -> bool`: writes the code to a temp `.mmd`, runs `mmdc -i … -o
    …`, returns True on exit 0 + output produced, False otherwise (O3). Locates `mmdc` via
    the local `node_modules/.bin` or PATH; if not found, logs a warning and treats as
    "unvalidated" (skip strict check, embed as-is).
  - On invalid: one regeneration passing the parser error back to the model (O4); if the
    retry also fails, return the raw block anyway (graceful degrade) with a warning.
- **Summarize integration**: after `summarize_transcript`, call the diagram step and
  **prepend** an `## Overview` section (O2) into `summary.md`, so the local artifact is
  complete and export needs no special-casing.
- **Export fix (one line)**: add `"mermaid": "mermaid"` to `_NOTION_LANGS` in
  `export_notion.py` so the fenced block renders as a diagram, not flattened to plain text.
- **Config**: reuse existing Azure OpenAI (`gpt-5.6-sol`) — no new creds. `mmdc` is an
  external Node tool (README setup), optional at runtime (degrades if absent).
- **Workspace/pipeline**: no new artifact (diagram lives inside summary.md). The diagram
  step runs inside the summarize stage; always-on (O1).
- **Errors**: reuse `SummarizationError` (or a small `DiagramError`); graceful-degrade per
  O4 — the diagram never blocks the summary.

## 10. Error handling

- Diagram model call failure → retry once; then degrade (ship summary without diagram +
  warn) per O4.
- Malformed Mermaid after retry → same graceful path; never block the summary.
- Export: an unknown/again-malformed mermaid block still renders as a code block in Notion
  (readable, just not a picture) — acceptable fallback.

## 11. Success criteria

- Every summary opens with one faithful, legible Mermaid overview that renders as a diagram
  in Notion.
- Diagram type suits the content (matrix-driven), labels are real and in the summary's
  language.
- Negligible added cost/latency (one text call).
- Unit-tested: diagram step (mocked model), the `mermaid` export-language mapping, graceful
  degradation on failure.
- Live-verified on the real Agent Memory bilibili summary.

## 12. Risks / notes

- **Diagram sprawl** — the model may over-node a complex video; the specialist's <20-node
  rule + our instruction must enforce brevity, else the "overview" becomes a hairball.
- **Mermaid syntax fragility** — one bad character breaks rendering; hence light validation
  + retry, and the code-block fallback.
- **Notion mermaid rendering** is confirmed working now, but is a Notion feature we don't
  control; the code-block fallback keeps output readable if it ever changes.
- **Label language** — must steer the model to the summary's dominant language explicitly,
  or it may default to English labels on Chinese content.
