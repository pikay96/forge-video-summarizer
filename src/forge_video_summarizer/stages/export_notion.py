"""Stage 5 — Export the summary to Notion (official notion-client SDK).

Creates (or updates) a **subpage** under a configured parent page. Each subpage
contains: an embedded bilibili video, a metadata callout, and the summary body
rendered as native Notion blocks. Audio and transcript are intentionally excluded.

Idempotency: we dedup by the video id embedded in the subpage title, so re-exporting
the same video archives the old subpage wholesale (one call) and creates a fresh one
rather than piling up duplicates.

Markdown -> blocks covers the shapes Stage 4 emits: H1/H2/H3, paragraphs, bulleted
and numbered lists, fenced code, and blockquotes. Inline `[MM:SS]` / `[HH:MM:SS]`
anchors are kept as plain text — bilibili's web player ignores `?t=<sec>` deep links
on click, so a hyperlink would just land at the start and mislead.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ExportError
from ..models import VideoMetadata

log = logging.getLogger(__name__)

__all__ = [
    "export_summary",
    "markdown_to_blocks",
    "NOTION_BLOCK_LIMIT",
]

NOTION_BLOCK_LIMIT = 100  # max blocks per children.append request

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# Inline segments, matched in priority order: code `x` and equations \(x\)/$x$ are
# LITERAL (no formatting inside); then **bold**/__bold__ and *italic*/_italic_.
_INLINE_SEG_RE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\\\((?P<eqp>.+?)\\\)"
    r"|(?<![\\$])\$(?P<eqd>(?=\S)[^$\n]+?(?<=\S))\$(?!\$)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|(?<![\w*])\*(?P<ital>\S(?:[^*\n]*?\S)?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<ital2>\S(?:[^_\n]*?\S)?)_(?![\w_])"
)


def _text_span(content: str, *, code: bool = False, bold: bool = False,
               italic: bool = False) -> dict:
    span: dict[str, Any] = {"type": "text", "text": {"content": content}}
    ann = {}
    if code:
        ann["code"] = True
    if bold:
        ann["bold"] = True
    if italic:
        ann["italic"] = True
    if ann:
        span["annotations"] = ann
    return span


def _equation_span(expr: str) -> dict:
    return {"type": "equation", "equation": {"expression": expr.strip()}}


def _rich_text(text: str) -> list[dict]:
    """Build rich_text spans, honoring inline code (`x`), inline equations
    (\\(x\\) or $x$ -> Notion equation spans), and **bold**/*italic* markdown.
    `[MM:SS]` anchors stay plain text (bilibili ignores ?t= deep links)."""
    spans: list[dict] = []
    pos = 0
    for m in _INLINE_SEG_RE.finditer(text):
        if m.start() > pos:
            spans.append(_text_span(text[pos : m.start()]))
        if m.group("code") is not None:
            spans.append(_text_span(m.group("code"), code=True))
        elif (eq := m.group("eqp") or m.group("eqd")) is not None:
            spans.append(_equation_span(eq))
        elif (b := m.group("bold") or m.group("bold2")) is not None:
            spans.append(_text_span(b, bold=True))
        else:
            spans.append(_text_span(m.group("ital") or m.group("ital2"), italic=True))
        pos = m.end()
    if pos < len(text):
        spans.append(_text_span(text[pos:]))
    return spans or [_text_span("")]


def _para(text: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def _heading(level: int, text: str) -> dict:
    key = f"heading_{min(level, 3)}"
    return {"type": key, key: {"rich_text": _rich_text(text)}}


def _list_item(kind: str, text: str) -> dict:
    key = "bulleted_list_item" if kind == "ul" else "numbered_list_item"
    return {"type": key, key: {"rich_text": _rich_text(text)}}


def _equation_block(expr: str) -> dict:
    return {"type": "equation", "equation": {"expression": expr.strip()}}


# Block equation delimiters: \[ ... \] or $$ ... $$ (whole line = one equation block).
_BLOCK_EQ_INLINE = re.compile(r"^\\\[\s*(.*?)\s*\\\]$|^\$\$\s*(.*?)\s*\$\$$")


def markdown_to_blocks(markdown: str) -> list[dict]:
    """Convert Stage-4 markdown into a list of Notion block objects."""
    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        fence = _FENCE_RE.match(stripped)
        if fence:
            lang = fence.group(1) or "plain text"
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append({
                "type": "code",
                "code": {
                    "language": _notion_lang(lang),
                    "rich_text": [_text_span("\n".join(code_lines))],
                },
            })
            continue

        # Block equation on a single line: \[ ... \] or $$ ... $$
        if m := _BLOCK_EQ_INLINE.match(stripped):
            expr = m.group(1) if m.group(1) is not None else m.group(2)
            if expr:
                blocks.append(_equation_block(expr))
                i += 1
                continue

        # Block equation spanning lines: opener \[ or $$, body, closer \] or $$
        if stripped in ("\\[", "$$"):
            closer = "\\]" if stripped == "\\[" else "$$"
            eq_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != closer:
                eq_lines.append(lines[i])
                i += 1
            i += 1  # skip closer
            expr = "\n".join(eq_lines).strip()
            if expr:
                blocks.append(_equation_block(expr))
            continue

        if not stripped:
            i += 1
            continue

        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(_heading(hashes, stripped[hashes:].strip()))
        elif stripped.startswith(("- ", "* ", "+ ")):
            blocks.append(_list_item("ul", stripped[2:].strip()))
        elif re.match(r"^\d+\.\s", stripped):
            blocks.append(_list_item("ol", re.sub(r"^\d+\.\s", "", stripped)))
        elif stripped.startswith(">"):
            blocks.append({
                "type": "quote",
                "quote": {"rich_text": _rich_text(stripped[1:].strip())},
            })
        else:
            blocks.append(_para(stripped))
        i += 1
    return blocks


_NOTION_LANGS = {
    "py": "python", "python": "python", "js": "javascript", "ts": "typescript",
    "json": "json", "bash": "bash", "sh": "shell", "shell": "shell", "text": "plain text",
}


def _notion_lang(lang: str) -> str:
    return _NOTION_LANGS.get(lang.lower(), "plain text")


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _header_blocks(metadata: VideoMetadata | None, video_url: str) -> list[dict]:
    """Video embed + a metadata callout, placed at the top of the page."""
    blocks: list[dict] = []
    if video_url:
        blocks.append({"type": "embed", "embed": {"url": video_url}})
    if metadata:
        lines = []
        if metadata.uploader:
            lines.append(f"UP主: {metadata.uploader}")
        lines.append(f"时长: {_fmt_duration(metadata.duration)}")
        if metadata.video_id:
            lines.append(f"Video ID: {metadata.video_id}")
        if video_url:
            lines.append(f"原视频: {video_url}")
        blocks.append({
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📺"},
                "rich_text": [_text_span("\n".join(lines))],
            },
        })
    blocks.append({"type": "divider", "divider": {}})
    return blocks


def _title_with_id(title: str, video_id: str) -> str:
    """Subpage title carrying the video id as a stable dedup marker."""
    return f"{title} [{video_id}]" if video_id else title


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _find_existing(client: Any, parent_id: str, marker_title: str) -> str | None:
    """Find a direct child subpage whose title matches (dedup). Returns page id."""
    try:
        cursor = None
        while True:
            resp = client.blocks.children.list(block_id=parent_id, start_cursor=cursor)
            for child in resp.get("results", []):
                if child.get("type") == "child_page" and \
                        child["child_page"].get("title") == marker_title:
                    return child["id"]
            if not resp.get("has_more"):
                return None
            cursor = resp.get("next_cursor")
    except Exception:  # noqa: BLE001 - dedup is best-effort; fall back to create
        return None


def _archive_page(client: Any, page_id: str) -> None:
    """Archive (soft-delete) an entire subpage in ONE call. Far cheaper than
    deleting child blocks individually, and avoids rate-limit storms on long pages."""
    with contextlib.suppress(Exception):  # best-effort; a fresh page is still created
        client.pages.update(page_id=page_id, archived=True)


def _upload_image_block(client: Any, image_path: Path) -> dict | None:
    """Upload a local image to Notion via file_uploads and return an `image` block that
    references it, or None if the upload fails (best-effort — the page still exports)."""
    try:
        name = image_path.name
        mime = mimetypes.guess_type(name)[0] or "image/png"
        up = client.file_uploads.create(mode="single_part", filename=name, content_type=mime)
        with image_path.open("rb") as fh:
            client.file_uploads.send(file_upload_id=up["id"], file=(name, fh, mime))
        return {
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": up["id"]}},
        }
    except Exception as exc:  # noqa: BLE001 - overview image is additive; never block export
        log.warning("overview image upload failed: %s", exc)
        return None


def export_summary(
    summary_markdown: str,
    config: Config,
    *,
    metadata: VideoMetadata | None = None,
    overview_image: Path | None = None,
    client: Any | None = None,
) -> str:
    """Create/update a Notion subpage for this summary. Returns the page URL.

    Idempotency strategy: if a subpage with the same video-id marker title already
    exists under the parent, archive it wholesale (one call) and create a fresh page.
    This is dramatically faster than clearing hundreds of child blocks one-by-one.
    """
    config.require_notion()
    if not summary_markdown.strip():
        raise ExportError("Summary is empty; nothing to export")

    client = client or _make_client(config)
    parent_id = config.notion_parent_id
    video_url = metadata.source_url if metadata else ""
    video_id = metadata.video_id if metadata else ""
    raw_title = metadata.title if metadata else "Video Summary"
    marker_title = _title_with_id(raw_title, video_id)

    all_blocks = _header_blocks(metadata, video_url) + markdown_to_blocks(summary_markdown)

    # Overview image at the very top (best-effort upload; skipped on failure).
    if overview_image is not None and Path(overview_image).is_file():
        img_block = _upload_image_block(client, Path(overview_image))
        if img_block is not None:
            all_blocks = [img_block, *all_blocks]

    first, rest = all_blocks[:NOTION_BLOCK_LIMIT], all_blocks[NOTION_BLOCK_LIMIT:]

    try:
        existing = _find_existing(client, parent_id, marker_title)
        if existing:
            _archive_page(client, existing)  # one call, then recreate fresh
        page = client.pages.create(
            parent={"type": "page_id", "page_id": parent_id},
            properties={"title": [{"type": "text", "text": {"content": marker_title}}]},
            children=first,
        )
        page_id = page["id"]
        for chunk in _chunked(rest, NOTION_BLOCK_LIMIT):
            client.blocks.children.append(block_id=page_id, children=chunk)
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001 - uniform surface for SDK/API errors
        raise ExportError(f"Notion export failed: {exc}") from exc

    return _page_url(page_id)


def _page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _make_client(config: Config):
    try:
        from notion_client import Client
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExportError("The 'notion-client' package is required for export") from exc
    return Client(auth=config.notion_key)
