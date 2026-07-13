# PRD — Overview diagram (Mermaid) in the summary

**Status:** Draft · **Owner:** Pikay · **Depends on:** Stage 4 (summarize), Stage 5
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

## 8. Open decisions (to lock)

- **O1 — Always-on or flag?** Is the overview diagram part of the *default* summarize
  output, or gated behind a flag (e.g. `--diagram`)? Unlike the heavy visual-frames
  feature, this is cheap (one text call, no images) — leaning **always-on**, since it
  strictly improves the summary at negligible cost. Confirm.
- **O2 — Placement.** Top of the summary (an `## Overview` section before the prose) vs
  bottom (a "big picture" recap). Leaning **top** — it's a map you read before diving in.
- **O3 — Validation.** Do we validate the Mermaid renders (e.g. a syntax sanity check /
  optional `mermaid.live`-style parse) and retry on failure, or trust the model + let
  Notion show a code block if it's malformed? Leaning **light validation**: a cheap
  structural check (correct fenced block, known diagram header, balanced brackets) with one
  regen retry; no heavyweight renderer dependency.
- **O4 — Failure behavior.** If diagram generation fails after retry, does the summary ship
  without it (degrade gracefully, warn) or is it a hard error? Leaning **graceful** — the
  text summary is the primary deliverable; the diagram is additive.
- **O5 — Language of labels.** Match the summary's dominant language (Chinese for your
  content) so the diagram reads consistently? Leaning **yes, match summary language.**

## 9. Proposed design (pending O1–O5)

- **New step — `stages/diagram.py`**: `generate_overview_diagram(summary_markdown, config,
  *, client=None) -> str` returns a Mermaid code block. Loads the specialist prompt via
  `importlib.resources.files("forge_video_summarizer.prompts")`, sends it as `instructions`,
  passes the finished summary as input, requests exactly one fenced ```mermaid block. Light
  validation + one retry (O3).
- **Summarize integration**: after `summarize_transcript`, call the diagram step and prepend
  an `## Overview` section (O2) to `summary.md`. Or keep it a distinct artifact merged at
  export time — decide during build (prefer prepend so the local summary.md is complete).
- **Export fix (one line)**: add `"mermaid": "mermaid"` to `_notion_lang`'s
  `_NOTION_LANGS` map in `export_notion.py` so the block renders as a diagram, not flattened
  to plain text. (The markdown→blocks code path already handles fenced code.)
- **Config**: reuse existing Azure OpenAI config (`gpt-5.6-sol`) — no new creds.
- **Workspace/pipeline**: no new artifact needed if prepended into summary.md; otherwise a
  `diagram.mmd`. Pipeline runs the step inside the summarize stage.
- **Errors**: `SummarizationError` reused (or a small `DiagramError`), graceful-degrade per
  O4.

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
