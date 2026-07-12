"""Stage 5 — Export the summary to Notion (official notion-client SDK).

Creates (or updates) a **subpage** under a configured parent page. Each subpage
contains: an embedded bilibili video, a metadata callout, and the summary body
rendered as native Notion blocks. Audio and transcript are intentionally excluded.

Idempotency: we dedup by the video id embedded in the subpage title, so re-exporting
the same video archives the old subpage's content and rewrites it rather than
piling up duplicates.

Markdown -> blocks covers the shapes Stage 4 emits: H1/H2/H3, paragraphs, bulleted
and numbered lists, fenced code, and blockquotes. Inline `[MM:SS]` / `[HH:MM:SS]`
anchors become clickable links into the bilibili video at that offset (`?t=<sec>`).
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Config
from ..errors import ExportError
from ..models import VideoMetadata

__all__ = [
    "export_summary",
    "markdown_to_blocks",
    "linkify_timestamps",
    "NOTION_BLOCK_LIMIT",
]

NOTION_BLOCK_LIMIT = 100  # max blocks per children.append request

# [MM:SS] or [HH:MM:SS], optionally already the section-heading leading anchor.
_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")
_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _ts_to_seconds(m: re.Match) -> int:
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is not None:  # HH:MM:SS
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)  # MM:SS


def linkify_timestamps(text: str, video_url: str) -> list[dict]:
    """Return Notion rich_text spans, turning [MM:SS] anchors into links to
    `<video_url>?t=<seconds>`. Without a video_url the anchors stay plain text.
    """
    spans: list[dict] = []
    pos = 0
    for m in _TS_RE.finditer(text):
        if m.start() > pos:
            spans.append(_text_span(text[pos : m.start()]))
        label = m.group(0)
        if video_url:
            sec = _ts_to_seconds(m)
            sep = "&" if "?" in video_url else "?"
            spans.append(_text_span(label, link=f"{video_url}{sep}t={sec}"))
        else:
            spans.append(_text_span(label))
        pos = m.end()
    if pos < len(text):
        spans.append(_text_span(text[pos:]))
    return spans or [_text_span("")]


def _text_span(content: str, *, link: str | None = None, code: bool = False) -> dict:
    span: dict[str, Any] = {"type": "text", "text": {"content": content}}
    if link:
        span["text"]["link"] = {"url": link}
    if code:
        span["annotations"] = {"code": True}
    return span


def _rich_text(text: str, video_url: str) -> list[dict]:
    """Build rich_text spans with inline-code (`x`) and timestamp links honored."""
    spans: list[dict] = []
    pos = 0
    for m in _INLINE_CODE_RE.finditer(text):
        if m.start() > pos:
            spans.extend(linkify_timestamps(text[pos : m.start()], video_url))
        spans.append(_text_span(m.group(1), code=True))
        pos = m.end()
    if pos < len(text):
        spans.extend(linkify_timestamps(text[pos:], video_url))
    return spans or [_text_span("")]


def _para(text: str, video_url: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _rich_text(text, video_url)}}


def _heading(level: int, text: str, video_url: str) -> dict:
    key = f"heading_{min(level, 3)}"
    return {"type": key, key: {"rich_text": _rich_text(text, video_url)}}


def _list_item(kind: str, text: str, video_url: str) -> dict:
    key = "bulleted_list_item" if kind == "ul" else "numbered_list_item"
    return {"type": key, key: {"rich_text": _rich_text(text, video_url)}}


def markdown_to_blocks(markdown: str, video_url: str = "") -> list[dict]:
    """Convert Stage-4 markdown into a list of Notion block objects."""
    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

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

        if not stripped:
            i += 1
            continue

        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(_heading(hashes, stripped[hashes:].strip(), video_url))
        elif stripped.startswith(("- ", "* ", "+ ")):
            blocks.append(_list_item("ul", stripped[2:].strip(), video_url))
        elif re.match(r"^\d+\.\s", stripped):
            blocks.append(_list_item("ol", re.sub(r"^\d+\.\s", "", stripped), video_url))
        elif stripped.startswith(">"):
            blocks.append({
                "type": "quote",
                "quote": {"rich_text": _rich_text(stripped[1:].strip(), video_url)},
            })
        else:
            blocks.append(_para(stripped, video_url))
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


def _archive_children(client: Any, page_id: str) -> None:
    """Archive all existing child blocks of a page (clears it before rewrite)."""
    cursor = None
    ids: list[str] = []
    while True:
        resp = client.blocks.children.list(block_id=page_id, start_cursor=cursor)
        ids.extend(c["id"] for c in resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    for bid in ids:
        try:
            client.blocks.delete(block_id=bid)
        except Exception:  # noqa: BLE001 - continue clearing best-effort
            pass


def export_summary(
    summary_markdown: str,
    config: Config,
    *,
    metadata: VideoMetadata | None = None,
    client: Any | None = None,
) -> str:
    """Create/update a Notion subpage for this summary. Returns the page URL."""
    config.require_notion()
    if not summary_markdown.strip():
        raise ExportError("Summary is empty; nothing to export")

    client = client or _make_client(config)
    parent_id = config.notion_parent_id
    video_url = metadata.source_url if metadata else ""
    video_id = metadata.video_id if metadata else ""
    raw_title = metadata.title if metadata else "Video Summary"
    marker_title = _title_with_id(raw_title, video_id)

    all_blocks = _header_blocks(metadata, video_url) + markdown_to_blocks(
        summary_markdown, video_url
    )
    first, rest = all_blocks[:NOTION_BLOCK_LIMIT], all_blocks[NOTION_BLOCK_LIMIT:]

    try:
        existing = _find_existing(client, parent_id, marker_title)
        if existing:
            _archive_children(client, existing)
            page_id = existing
            client.blocks.children.append(block_id=page_id, children=first)
        else:
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
