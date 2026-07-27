from __future__ import annotations

from unittest.mock import patch

from forge_video_summarizer.config import Config
from forge_video_summarizer.stages.frames import SlideCandidate
from forge_video_summarizer.stages.slides import (
    SLIDE_PLACEHOLDER_RE,
    _parse_placements,
    _section_anchors,
    place_slides,
    select_slide_placements,
)

SUMMARY = """# Title

## Walkthrough

### [00:06] Intro

Some prose.

### [02:57] Multi-head attention

More prose.

### [04:32] GQA
"""


def test_section_anchors_extracted():
    assert _section_anchors(SUMMARY) == ["00:06", "02:57", "04:32"]


def test_parse_placements_validates_and_dedupes():
    reply = '[{"slide":"02:57","section":"02:57"},{"slide":"02:57","section":"00:06"},' \
            '{"slide":"99:99","section":"00:06"},{"slide":"04:32","section":"zzz"}]'
    out = _parse_placements(reply, {"02:57", "04:32"}, {"00:06", "02:57", "04:32"})
    # dup 02:57 kept once; unknown slide 99:99 and unknown section zzz dropped
    assert out == [{"slide": "02:57", "section": "02:57"}]


def test_parse_placements_strips_code_fence():
    reply = "```json\n[{\"slide\":\"00:06\",\"section\":\"00:06\"}]\n```"
    out = _parse_placements(reply, {"00:06"}, {"00:06"})
    assert out == [{"slide": "00:06", "section": "00:06"}]


def test_parse_placements_garbage_returns_empty():
    assert _parse_placements("no json here", {"00:06"}, {"00:06"}) == []


def test_place_slides_inserts_after_matching_heading():
    placements = [{"slide": "02:57", "section": "02:57"}, {"slide": "04:32", "section": "04:32"}]
    out = place_slides(SUMMARY, placements)
    lines = out.splitlines()
    # placeholder appears right under the matching heading
    idx = lines.index("### [02:57] Multi-head attention")
    assert "![slide@02:57]" in lines[idx : idx + 3]
    assert SLIDE_PLACEHOLDER_RE.search(out)
    # unrelated heading has no placeholder immediately under it
    intro = lines.index("### [00:06] Intro")
    assert not any(SLIDE_PLACEHOLDER_RE.search(ln) for ln in lines[intro : intro + 2])


def test_place_slides_noop_without_placements():
    assert place_slides(SUMMARY, []) == SUMMARY


def test_select_returns_empty_without_candidates_or_anchors():
    cfg = Config()
    assert select_slide_placements([], SUMMARY, cfg, client=object()) == []
    cand = [SlideCandidate(timestamp=6.0, path=__import__("pathlib").Path("x.png"))]
    assert select_slide_placements(cand, "no headings here", cfg, client=object()) == []


def test_select_slide_placements_happy_path(tmp_path):
    # a real image file so _data_url succeeds
    img = tmp_path / "slide_02-57.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakePNGbytes")
    cands = [SlideCandidate(timestamp=177.0, path=img)]  # 177s -> "02:57"
    cfg = Config(openai_endpoint="e", openai_key="k")
    reply = '[{"slide":"02:57","section":"02:57"}]'
    with patch(
        "forge_video_summarizer.stages.slides.call_responses_vision", return_value=reply
    ) as vc:
        out = select_slide_placements(cands, SUMMARY, cfg, client=object())
    assert out == [{"slide": "02:57", "section": "02:57"}]
    # the vision call got one image labeled with the slide timestamp
    _, _, _, _, images = vc.call_args.args
    assert images and images[0][0] == "SLIDE 02:57"
    assert images[0][1].startswith("data:image/")
