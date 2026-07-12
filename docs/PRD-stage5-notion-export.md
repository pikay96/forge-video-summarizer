# PRD — Stage 5: Export to Notion

**Status:** Draft · **Owner:** Pikay · **Depends on:** Stage 4 (summary.md)

## 1. Problem / Why

The pipeline produces a local `summary.md` per video. To be *useful* — searchable,
revisitable, shareable, browsable on mobile — summaries should live in Notion, not
scattered in `output/` folders. Stage 5 publishes each finished summary to a Notion
database as a richly-formatted page.

## 2. Goal

`fvs export <source>` (and an `--export` flag on `fvs summarize`) takes the finished
summary + metadata and creates/updates a Notion page in a target database, returning
the page URL.

## 3. Scope (v1)

**In:**
- Publish one video's `summary.md` → one Notion page in a configured database.
- Map metadata → page properties (Title, Source URL, Duration, Video ID, Date, Language).
- Render the markdown body as native Notion blocks (headings, bullets, code, etc.).
- Idempotent: re-exporting the same video updates its existing page (no duplicates).
- Return + print the page URL.

**Out (v1):**
- No embedding the video/thumbnail (can add later).
- No syncing edits back from Notion → local.
- No multi-database routing / tagging taxonomy beyond basic properties.
- No batch/playlist export (single video, consistent with the rest of v1).

## 4. Decisions (LOCKED)

- **D1 — Structure:** ✅ **Single parent page**, provided by user. Every summary is a
  **subpage** (child page) of that parent. No database.
- **D2 — Auth:** ✅ **Notion internal integration API key** in `.env` (`NOTION_TOKEN`).
  The integration must be shared with the parent page.
- **D3 — Page contents:** ✅ Each summary subpage contains: the **video** (embedded),
  **metadata**, and the **summary body**. **NOT** audio, **NOT** the transcript.
- **D4 — Dedup:** match an existing subpage by title/video-id among the parent's
  children; update in place rather than creating a duplicate.
- **D5 — Markdown fidelity (v1):** headings, paragraphs, bullets, numbered lists, code,
  quotes. Flatten anything more exotic. ✅ Timestamp anchors → **clickable
  `bilibili.com/BV...?t=<sec>` links** (confirmed).
- **D6 — Media / SDK:** ✅ CONFIRMED the Notion API supports media. ✅ **Decision: embed
  the bilibili URL** as a `video`/`embed` block (light — no 44MB upload per run; player
  renders inline). File Uploads API left for a future "host mp4 in Notion" option.
  ✅ **Use the official `notion-client` SDK** (handles auth, `Notion-Version` header,
  retries, pagination) — added to `dependencies`.

## 4b. Config inputs still needed from user

- `NOTION_TOKEN` — internal integration secret.
- `NOTION_PARENT_PAGE_ID` — the parent page every summary nests under.
- (The integration must be **shared/connected** to that parent page in Notion UI.)

## 5. Proposed design

- New stage module `stages/export_notion.py` + `NotionExporter`.
- New errors: `ExportError(ForgeError)`.
- Config: `NOTION_TOKEN`, `NOTION_DATABASE_ID` in `.env` + `require_notion()`.
- Pipeline: `run_export(ws, force=False)` reads `summary.md` + `metadata.json`,
  builds properties + blocks, upserts the page, writes the returned URL into the
  workspace (e.g. `notion_url.txt`) for cache/idempotency.
- CLI: `fvs export <source>`; extend `run_all` with optional export.
- Markdown→blocks: a small, tested converter (no heavyweight dep for the parsing).
- Notion access via the official **`notion-client`** SDK (`Client(auth=NOTION_TOKEN)`).

## 6. Error handling (consistent with existing stages)

- Fail fast: `require_notion()` before any API call.
- Map Notion API failures (4xx/5xx, rate limits) → `ExportError` with status + body.
- Idempotency: query-then-update guards against duplicate pages.
- Notion's 100-blocks-per-request limit → chunk block appends.

## 7. Success criteria

- `fvs export <url>` creates a well-formatted, readable Notion page and prints its URL.
- Re-running updates the same page (no duplicate).
- Unit tests cover: property mapping, markdown→blocks conversion, upsert logic
  (mocked Notion API), error paths.
- Live-verified on the real bilibili video's summary.

## 8. Risks / notes

- Notion block schema is verbose and picky; the markdown→blocks converter is the
  main complexity + test surface.
- Rate limits (~3 req/s) — chunked appends should stay well under.
- Notion API pins a version header (`Notion-Version`) — pin a known-good one.
