from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from forge_video_summarizer.config import Config
from forge_video_summarizer.stages.frames import SlideCandidate
from forge_video_summarizer.stages.slides import (
    SLIDE_PLACEHOLDER_RE,
    _clean_box,
    _clean_fraction,
    _parse_placements,
    _section_anchors,
    clean_selected_frames,
    place_slides,
    select_slide_placements,
)

# Concept-organized summary: headings are CONCEPTS, anchors live inline in prose.
SUMMARY = """# Title

## Walkthrough

### Why generation must cache K and V

Every step reuses past keys and values [00:06], so they are cached.

### MHA vs MQA vs GQA

MQA shares one KV set across heads [02:57], while GQA groups them [04:32].

### The final formula

Putting it together gives the full expression [06:40].
"""


def test_section_anchors_collects_inline_anchors():
    assert _section_anchors(SUMMARY) == ["00:06", "02:57", "04:32", "06:40"]


def test_section_anchors_dedupes():
    assert _section_anchors("a [01:00] b [01:00] c [02:00]") == ["01:00", "02:00"]


def test_parse_placements_validates_and_dedupes():
    reply = '[{"slide":"02:57","section":"02:57"},{"slide":"02:57","section":"00:06"},' \
            '{"slide":"99:99","section":"00:06"},{"slide":"04:32","section":"zzz"}]'
    out = _parse_placements(reply, {"02:57", "04:32"}, {"00:06", "02:57", "04:32"})
    assert len(out) == 1
    assert out[0]["slide"] == "02:57" and out[0]["section"] == "02:57"


def test_parse_placements_extracts_cleanup_fields():
    reply = '[{"slide":"02:57","section":"02:57","chrome_bottom":0.09,' \
            '"webcam":[0.8,0.99,0.02,0.2]}]'
    out = _parse_placements(reply, {"02:57"}, {"02:57"})
    assert out[0]["chrome_bottom"] == 0.09
    assert out[0]["webcam"] == (0.8, 0.99, 0.02, 0.2)


def test_parse_placements_sanitizes_bad_cleanup():
    reply = '[{"slide":"02:57","section":"02:57","chrome_bottom":5,"webcam":[1,0,2,3]}]'
    out = _parse_placements(reply, {"02:57"}, {"02:57"})
    assert out[0]["chrome_bottom"] == 0.0  # out of range -> ignored
    assert out[0]["webcam"] is None  # inverted/out-of-bounds -> ignored


def test_clean_fraction_and_box_helpers():
    assert _clean_fraction("0.1") == 0.1
    assert _clean_fraction(None) == 0.0
    assert _clean_fraction(1.5) == 0.0
    assert _clean_box([0.1, 0.2, 0.3, 0.4]) == (0.1, 0.2, 0.3, 0.4)
    assert _clean_box([0.2, 0.1, 0.3, 0.4]) is None  # x0 >= x1
    assert _clean_box("nope") is None
    assert _clean_box([0.1, 0.2, 0.3]) is None


def test_parse_placements_strips_code_fence():
    reply = '```json\n[{"slide":"00:06","section":"00:06"}]\n```'
    out = _parse_placements(reply, {"00:06"}, {"00:06"})
    assert out and out[0]["slide"] == "00:06"


def test_parse_placements_garbage_returns_empty():
    assert _parse_placements("no json here", {"00:06"}, {"00:06"}) == []


def test_place_slides_inserts_after_inline_anchor_line():
    placements = [{"slide": "02:57", "section": "02:57"}]
    out = place_slides(SUMMARY, placements)
    lines = out.splitlines()
    anchor_line = next(i for i, ln in enumerate(lines) if "[02:57]" in ln and "slide@" not in ln)
    # placeholder follows the paragraph that carries the anchor
    assert "![slide@02:57]" in lines[anchor_line : anchor_line + 3]
    assert SLIDE_PLACEHOLDER_RE.search(out)


def test_place_slides_never_splits_heading_from_body():
    md = "### [01:00] Legacy heading style\n\nBody text [01:00] here."
    out = place_slides(md, [{"slide": "01:00", "section": "01:00"}])
    lines = out.splitlines()
    assert not lines[1].startswith("![slide@")  # not immediately after the heading


def test_place_slides_skips_unknown_anchor():
    out = place_slides(SUMMARY, [{"slide": "09:09", "section": "09:09"}])
    assert "![slide@" not in out


def test_place_slides_noop_without_placements():
    assert place_slides(SUMMARY, []) == SUMMARY


def test_select_returns_empty_without_candidates_or_anchors():
    cfg = Config()
    assert select_slide_placements([], SUMMARY, cfg, client=object()) == []
    cand = [SlideCandidate(timestamp=6.0, path=Path("x.png"))]
    assert select_slide_placements(cand, "no anchors here", cfg, client=object()) == []


def test_select_slide_placements_happy_path(tmp_path):
    img = tmp_path / "slide_02-57.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakePNGbytes")
    cands = [SlideCandidate(timestamp=177.0, path=img)]  # 177s -> "02:57"
    cfg = Config(openai_endpoint="e", openai_key="k")
    reply = '[{"slide":"02:57","section":"02:57"}]'
    with patch(
        "forge_video_summarizer.stages.slides.call_responses_vision", return_value=reply
    ) as vc:
        out = select_slide_placements(cands, SUMMARY, cfg, client=object())
    assert out and out[0]["slide"] == "02:57"
    _, _, _, _, images = vc.call_args.args
    assert images and images[0][0] == "SLIDE 02:57"
    assert images[0][1].startswith("data:image/")


def test_clean_selected_frames_applies_only_to_kept(tmp_path):
    kept = tmp_path / "slide_02-57.png"
    kept.write_bytes(b"x")
    other = tmp_path / "slide_00-06.png"
    other.write_bytes(b"x")
    cands = [
        SlideCandidate(timestamp=177.0, path=kept),
        SlideCandidate(timestamp=6.0, path=other),
    ]
    placements = [{"slide": "02:57", "section": "02:57",
                   "chrome_bottom": 0.08, "webcam": (0.8, 0.95, 0.0, 0.2)}]
    with patch("forge_video_summarizer.stages.slides.apply_cleanup") as ac:
        clean_selected_frames(cands, placements)
    ac.assert_called_once()
    assert ac.call_args.args[0] == kept
    assert ac.call_args.kwargs["chrome_bottom"] == 0.08


def test_clean_selected_frames_skips_when_nothing_to_clean(tmp_path):
    p = tmp_path / "slide_02-57.png"
    p.write_bytes(b"x")
    cands = [SlideCandidate(timestamp=177.0, path=p)]
    placements = [{"slide": "02:57", "section": "02:57", "chrome_bottom": 0.0, "webcam": None}]
    with patch("forge_video_summarizer.stages.slides.apply_cleanup") as ac:
        clean_selected_frames(cands, placements)
    ac.assert_not_called()
