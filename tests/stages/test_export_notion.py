from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from forge_video_summarizer.config import normalize_page_id
from forge_video_summarizer.errors import ConfigError, ExportError
from forge_video_summarizer.models import VideoMetadata
from forge_video_summarizer.stages.export_notion import (
    NOTION_BLOCK_LIMIT,
    export_summary,
    markdown_to_blocks,
)

VIDEO_URL = "https://www.bilibili.com/video/BV1nU96BnE6P"


# ── normalize_page_id ───────────────────────────────────────────────────────

def test_normalize_from_url():
    url = "https://app.notion.com/p/pikameow/Video-Summarizer-39b0ddcb890880caadc4cf292e18f46e"
    assert normalize_page_id(url) == "39b0ddcb-8908-80ca-adc4-cf292e18f46e"


def test_normalize_from_bare_hex():
    assert normalize_page_id("39b0ddcb890880caadc4cf292e18f46e") == \
        "39b0ddcb-8908-80ca-adc4-cf292e18f46e"


def test_normalize_from_dashed_uuid():
    u = "39b0ddcb-8908-80ca-adc4-cf292e18f46e"
    assert normalize_page_id(u) == u


def test_normalize_empty():
    assert normalize_page_id("") == ""


# ── timestamps stay plain text (bilibili ignores ?t= deep links) ─────────────

def test_timestamps_stay_plain_text():
    blocks = markdown_to_blocks("intro [01:45] then [1:02:03] end")
    spans = blocks[0]["paragraph"]["rich_text"]
    assert all("link" not in s["text"] for s in spans)
    assert "".join(s["text"]["content"] for s in spans) == "intro [01:45] then [1:02:03] end"


# ── markdown_to_blocks ──────────────────────────────────────────────────────

def test_headings_levels():
    blocks = markdown_to_blocks("# A\n## B\n### C\n#### D")
    assert [b["type"] for b in blocks] == [
        "heading_1", "heading_2", "heading_3", "heading_3",  # h4 clamps to h3
    ]


def test_lists_and_paragraph():
    md = "- one\n- two\n1. first\n2. second\n\nplain text"
    blocks = markdown_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert types == [
        "bulleted_list_item", "bulleted_list_item",
        "numbered_list_item", "numbered_list_item",
        "paragraph",
    ]


def test_code_fence():
    md = "```python\nprint(1)\nprint(2)\n```"
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "python"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "print(1)\nprint(2)"


def test_quote_and_inline_code():
    blocks = markdown_to_blocks("> quoted `code` here")
    assert blocks[0]["type"] == "quote"
    spans = blocks[0]["quote"]["rich_text"]
    assert any(s.get("annotations", {}).get("code") for s in spans)


def test_heading_timestamp_is_plain_text():
    blocks = markdown_to_blocks("### [00:00] Intro")
    spans = blocks[0]["heading_3"]["rich_text"]
    assert all("link" not in s["text"] for s in spans)
    assert "".join(s["text"]["content"] for s in spans) == "[00:00] Intro"


# ── LaTeX equations -> Notion equation blocks/spans ─────────────────────────

def test_block_equation_bracket_delimiters_multiline():
    md = "推导：\n\n\\[\n\\text{KV}=2 \\times n \\times L\n\\]\n\n完成。"
    blocks = markdown_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert types == ["paragraph", "equation", "paragraph"]
    assert blocks[1]["equation"]["expression"] == "\\text{KV}=2 \\times n \\times L"


def test_block_equation_single_line_both_delimiters():
    for md in ("\\[E=mc^2\\]", "$$E=mc^2$$"):
        blocks = markdown_to_blocks(md)
        assert blocks[0]["type"] == "equation"
        assert blocks[0]["equation"]["expression"] == "E=mc^2"


def test_inline_equations_become_equation_spans():
    blocks = markdown_to_blocks(r"当 \(H_{KV}=8\) 且 $B=1$ 时。")
    spans = blocks[0]["paragraph"]["rich_text"]
    eqs = [s["equation"]["expression"] for s in spans if s["type"] == "equation"]
    assert eqs == ["H_{KV}=8", "B=1"]
    # surrounding prose survives as text spans
    assert any(s["type"] == "text" and "当" in s["text"]["content"] for s in spans)


def test_bare_dollar_amounts_not_treated_as_equations():
    blocks = markdown_to_blocks("花了 $5 和 $10 元。")
    spans = blocks[0]["paragraph"]["rich_text"]
    assert all(s["type"] == "text" for s in spans)


# ── bold / italic inline markdown -> Notion annotations ─────────────────────

def _ann_tags(spans):
    out = []
    for s in spans:
        if s["type"] != "text":
            out.append(("eq", s["equation"]["expression"]))
            continue
        a = s.get("annotations", {})
        tag = "b" if a.get("bold") else "i" if a.get("italic") else "c" if a.get("code") else "-"
        out.append((tag, s["text"]["content"]))
    return out


def test_bold_at_line_start_then_cjk():
    # exact shape from the report: **X（...)**介于... and list "**MQA** 是..."
    b = markdown_to_blocks("**Grouped-Query Attention（GQA）**介于 MHA 之间。")
    tags = _ann_tags(b[0]["paragraph"]["rich_text"])
    assert tags[0] == ("b", "Grouped-Query Attention（GQA）")
    assert tags[1][0] == "-" and "介于" in tags[1][1]


def test_bold_in_list_item():
    b = markdown_to_blocks("- **GQA** 则处于两者之间。")
    tags = _ann_tags(b[0]["bulleted_list_item"]["rich_text"])
    assert tags[0] == ("b", "GQA")


def test_italic_but_not_multiplication_or_snake_case():
    b = markdown_to_blocks("这是 *斜体*，但 2 * n * d 和 d_head 不是。")
    tags = _ann_tags(b[0]["paragraph"]["rich_text"])
    assert ("i", "斜体") in tags
    # the "* n *" multiplication and snake_case must NOT produce italic spans
    assert not any(t == "i" and text != "斜体" for t, text in tags)


def test_bold_and_equation_coexist():
    b = markdown_to_blocks(r"**结论**：显存为 $2nd$。")
    tags = _ann_tags(b[0]["paragraph"]["rich_text"])
    assert ("b", "结论") in tags
    assert ("eq", "2nd") in tags


# ── export_summary (mocked notion-client) ───────────────────────────────────

def _fake_client(existing_children=None):
    client = MagicMock()
    client.blocks.children.list.return_value = {
        "results": existing_children or [], "has_more": False, "next_cursor": None,
    }
    client.pages.create.return_value = {"id": "newpage-id-0000"}
    client.file_uploads.create.return_value = {"id": "up-123", "status": "pending"}
    client.file_uploads.send.return_value = {"status": "uploaded"}
    return client


def _meta():
    return VideoMetadata(
        video_id="BV1nU96BnE6P", title="Agent Memory",
        source_url=VIDEO_URL, duration=1655.0, uploader="某UP主",
    )


def test_export_requires_config(config):
    config.notion_key = ""
    with pytest.raises(ConfigError, match="Notion not configured"):
        export_summary("# hi", config, metadata=_meta())


def test_export_empty_summary(config):
    with pytest.raises(ExportError, match="empty"):
        export_summary("   ", config, metadata=_meta(), client=_fake_client())


def test_export_creates_new_page(config):
    client = _fake_client(existing_children=[])
    url = export_summary("# Title\n\nbody", config, metadata=_meta(), client=client)
    client.pages.create.assert_called_once()
    kwargs = client.pages.create.call_args.kwargs
    # nests under the normalized parent id
    assert kwargs["parent"]["page_id"] == "39b0ddcb-8908-80ca-adc4-cf292e18f46e"
    # title carries the video id marker for dedup
    assert kwargs["properties"]["title"][0]["text"]["content"] == "Agent Memory [BV1nU96BnE6P]"
    assert "notion.so" in url


def test_export_updates_existing_page(config):
    existing = [{
        "id": "existing-id-1234", "type": "child_page",
        "child_page": {"title": "Agent Memory [BV1nU96BnE6P]"},
    }]
    client = _fake_client(existing_children=existing)
    export_summary("# Title\n\nbody", config, metadata=_meta(), client=client)
    # old page archived wholesale (one call), fresh page created — no duplicate title
    client.pages.update.assert_called_once_with(page_id="existing-id-1234", archived=True)
    client.pages.create.assert_called_once()


def test_export_chunks_large_block_lists(config):
    client = _fake_client(existing_children=[])
    big_md = "\n\n".join(f"paragraph {i}" for i in range(250))
    export_summary(big_md, config, metadata=_meta(), client=client)
    # create gets first<=100; remaining appended in <=100 chunks
    first = client.pages.create.call_args.kwargs["children"]
    assert len(first) <= NOTION_BLOCK_LIMIT
    for call in client.blocks.children.append.call_args_list:
        assert len(call.kwargs["children"]) <= NOTION_BLOCK_LIMIT


def test_export_wraps_api_errors(config):
    client = _fake_client(existing_children=[])
    client.pages.create.side_effect = RuntimeError("boom 401")
    with pytest.raises(ExportError, match="Notion export failed"):
        export_summary("# hi\n\nx", config, metadata=_meta(), client=client)


def test_export_embeds_overview_image_first(config, tmp_path):
    client = _fake_client(existing_children=[])
    png = tmp_path / "overview.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    export_summary("# Title\n\nbody", config, metadata=_meta(),
                   overview_image=png, client=client)
    # uploaded via file_uploads (create + send)
    client.file_uploads.create.assert_called_once()
    client.file_uploads.send.assert_called_once()
    # image block is the very first block on the page
    first = client.pages.create.call_args.kwargs["children"]
    assert first[0]["type"] == "image"
    assert first[0]["image"]["file_upload"]["id"] == "up-123"


def test_export_survives_overview_upload_failure(config, tmp_path):
    client = _fake_client(existing_children=[])
    client.file_uploads.create.side_effect = RuntimeError("upload boom")
    png = tmp_path / "overview.png"
    png.write_bytes(b"\x89PNG")
    # export still succeeds; no image block, page created normally
    url = export_summary("# Title\n\nbody", config, metadata=_meta(),
                         overview_image=png, client=client)
    assert url
    first = client.pages.create.call_args.kwargs["children"]
    assert first[0]["type"] != "image"


def test_export_no_overview_when_absent(config, tmp_path):
    client = _fake_client(existing_children=[])
    export_summary("# Title\n\nbody", config, metadata=_meta(),
                   overview_image=tmp_path / "missing.png", client=client)
    client.file_uploads.create.assert_not_called()
