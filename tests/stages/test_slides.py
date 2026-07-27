from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from forge_video_summarizer.config import Config
from forge_video_summarizer.stages.frames import SlideCandidate
from forge_video_summarizer.stages.slides import (
    SLIDE_PLACEHOLDER_RE,
    _dedupe_placements,
    _parse_placements,
    _section_anchors,
    _walkthrough_slice,
    place_slides,
    select_slide_placements,
)

# Concept-organized summary: headings are CONCEPTS, anchors live inline in prose.
SUMMARY = """# Title

## TL;DR

Quick point [09:99] that must never receive a slide.

## Walkthrough

### Why generation must cache K and V

Every step reuses past keys and values [00:06], so they are cached.

### MHA vs MQA vs GQA

MQA shares one KV set across heads [02:57], while GQA groups them [04:32].

### The final formula

Putting it together gives the full expression [06:40].

## Key takeaways

- Recap [08:88] which must never receive a slide.
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
    assert images and images[0][0].startswith("SLIDE 02:57")
    assert "caption" in images[0][0]  # score travels with the label
    assert images[0][1].startswith("data:image/")


def _fake_candidates(tmp_path, seconds):
    out = []
    for s in seconds:
        p = tmp_path / f"slide_{s // 60:02d}-{s % 60:02d}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        out.append(SlideCandidate(timestamp=float(s), path=p))
    return out


def test_select_batches_large_candidate_sets(tmp_path):
    """Every candidate must be shown to the model; too many at once made it drop slides."""
    cands = _fake_candidates(tmp_path, [6, 57, 177, 272, 315, 328, 411])
    cfg = Config(openai_endpoint="e", openai_key="k")
    with patch(
        "forge_video_summarizer.stages.slides.call_responses_vision", return_value="[]"
    ) as vc:
        select_slide_placements(cands, SUMMARY, cfg, client=object(), batch_size=3)
    assert vc.call_count == 3  # 7 candidates / batch of 3
    shown = [lbl for call in vc.call_args_list for lbl, _ in call.args[4]]
    assert len(shown) == 7  # nothing silently skipped
    assert any(lbl.startswith("SLIDE 05:15") for lbl in shown)


def test_select_merges_results_across_batches(tmp_path):
    cands = _fake_candidates(tmp_path, [6, 177, 315, 411])
    cfg = Config(openai_endpoint="e", openai_key="k")
    replies = [
        '[{"slide":"00:06","section":"00:06"}]',
        '[{"slide":"05:15","section":"04:32"}]',
    ]
    with patch(
        "forge_video_summarizer.stages.slides.call_responses_vision", side_effect=replies
    ):
        out = select_slide_placements(cands, SUMMARY, cfg, client=object(), batch_size=2)
    assert [p["slide"] for p in out] == ["00:06", "05:15"]


def test_select_survives_a_failing_batch(tmp_path):
    cands = _fake_candidates(tmp_path, [6, 177, 315, 411])
    cfg = Config(openai_endpoint="e", openai_key="k")
    with patch(
        "forge_video_summarizer.stages.slides.call_responses_vision",
        side_effect=[RuntimeError("boom"), '[{"slide":"05:15","section":"04:32"}]'],
    ):
        out = select_slide_placements(cands, SUMMARY, cfg, client=object(), batch_size=2)
    assert [p["slide"] for p in out] == ["05:15"]  # good batch still lands


def test_dedupe_placements_keeps_one_per_slide():
    dupes = [
        {"slide": "05:15", "section": "04:32"},
        {"slide": "05:15", "section": "06:40"},
        {"slide": "00:06", "section": "00:06"},
    ]
    out = _dedupe_placements(dupes)
    assert [p["slide"] for p in out] == ["00:06", "05:15"]



# ── screenshots are confined to the Walkthrough ─────────────────────────────

def test_walkthrough_slice_bounds_the_section():
    start, end = _walkthrough_slice(SUMMARY)
    inner = SUMMARY[start:end]
    assert inner.lstrip().startswith("## Walkthrough")
    assert "TL;DR" not in inner
    assert "Key takeaways" not in inner


def test_anchors_exclude_non_walkthrough_sections():
    anchors = _section_anchors(SUMMARY)
    assert "09:99" not in anchors  # TL;DR
    assert "08:88" not in anchors  # Key takeaways
    assert "02:57" in anchors


def test_walkthrough_slice_falls_back_to_whole_doc():
    md = "# Title\n\nNo walkthrough heading [01:00] here."
    assert _walkthrough_slice(md) == (0, len(md))


def test_place_slides_never_writes_outside_walkthrough():
    # even if a placement targets a TL;DR anchor, nothing may be inserted there
    out = place_slides(SUMMARY, [{"slide": "02:57", "section": "09:99"}])
    tldr = out.split("## Walkthrough")[0]
    assert "![slide@" not in tldr


def test_place_slides_not_added_to_takeaways():
    out = place_slides(SUMMARY, [{"slide": "02:57", "section": "08:88"}])
    tail = out.split("## Key takeaways")[-1]
    assert "![slide@" not in tail


# ── frame-quality correction (caption occlusion + edge cropping) ────────────

def _img(path, colour=(10, 10, 10), bar=None, edge_ink=False):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (200, 150), colour)
    d = ImageDraw.Draw(im)
    if bar:  # yellow caption band near the bottom
        d.rectangle(bar, fill=(255, 220, 60))
    if edge_ink:  # bright content running off both side edges
        d.rectangle((0, 40, 12, 110), fill=(240, 240, 240))
        d.rectangle((188, 40, 200, 110), fill=(240, 240, 240))
    im.save(path)
    return path


def test_caption_score_zero_without_caption(tmp_path):
    from forge_video_summarizer.stages.frames import _caption_score
    assert _caption_score(_img(tmp_path / "clean.png")) == 0.0


def test_caption_score_positive_with_caption(tmp_path):
    from forge_video_summarizer.stages.frames import _caption_score
    captioned = _img(tmp_path / "cap.png", bar=(20, 120, 180, 140))
    assert _caption_score(captioned) > 0.0


def test_edge_crop_score_flags_content_at_the_border(tmp_path):
    from forge_video_summarizer.stages.slides import _edge_crop_score
    clean = _edge_crop_score(_img(tmp_path / "a.png"))
    cropped = _edge_crop_score(_img(tmp_path / "b.png", edge_ink=True))
    assert cropped > clean


def test_prefer_cleaner_duplicates_swaps_to_the_better_frame(tmp_path):
    from forge_video_summarizer.stages.frames import SlideCandidate
    from forge_video_summarizer.stages.slides import _prefer_cleaner_duplicates

    bad = SlideCandidate(
        timestamp=315.0,
        path=_img(tmp_path / "slide_05-15.png", bar=(20, 120, 180, 140)),
    )
    good = SlideCandidate(timestamp=328.0, path=_img(tmp_path / "slide_05-28.png"))
    out = _prefer_cleaner_duplicates(
        [{"slide": "05:15", "section": "04:54"}], [bad, good]
    )
    assert out[0]["slide"] == "05:28"  # label follows the frame actually used


def test_prefer_cleaner_duplicates_ignores_distant_frames(tmp_path):
    from forge_video_summarizer.stages.frames import SlideCandidate
    from forge_video_summarizer.stages.slides import _prefer_cleaner_duplicates

    kept = SlideCandidate(
        timestamp=315.0,
        path=_img(tmp_path / "slide_05-15.png", bar=(20, 120, 180, 140)),
    )
    far = SlideCandidate(timestamp=600.0, path=_img(tmp_path / "slide_10-00.png"))
    out = _prefer_cleaner_duplicates(
        [{"slide": "05:15", "section": "04:54"}], [kept, far], window=20.0
    )
    assert out[0]["slide"] == "05:15"  # a different slide must never be substituted


def test_prefer_cleaner_duplicates_keeps_unknown_labels(tmp_path):
    from forge_video_summarizer.stages.slides import _prefer_cleaner_duplicates
    pl = [{"slide": "09:99", "section": "04:54"}]
    assert _prefer_cleaner_duplicates(pl, []) == pl


# ── unusable frames are dropped rather than shipped mutilated ───────────────

def test_reject_unusable_drops_heavily_cropped(tmp_path):
    from forge_video_summarizer.stages.frames import SlideCandidate
    from forge_video_summarizer.stages.slides import _reject_unusable_frames

    cropped = SlideCandidate(
        timestamp=315.0, path=_img(tmp_path / "slide_05-15.png", edge_ink=True)
    )
    out = _reject_unusable_frames([{"slide": "05:15", "section": "04:54"}], [cropped])
    assert out == []  # better no screenshot than half a slide


def test_reject_unusable_keeps_clean_frames(tmp_path):
    from forge_video_summarizer.stages.frames import SlideCandidate
    from forge_video_summarizer.stages.slides import _reject_unusable_frames

    clean = SlideCandidate(timestamp=315.0, path=_img(tmp_path / "slide_05-15.png"))
    pl = [{"slide": "05:15", "section": "04:54"}]
    assert _reject_unusable_frames(pl, [clean]) == pl


def test_reject_unusable_drops_caption_over_content(tmp_path):
    from forge_video_summarizer.stages.frames import SlideCandidate
    from forge_video_summarizer.stages.slides import _reject_unusable_frames

    # a wide caption band covering a busy region scores well above _MAX_CAPTION
    obscured = SlideCandidate(
        timestamp=315.0,
        path=_img(tmp_path / "slide_05-15.png", bar=(5, 110, 195, 145)),
    )
    out = _reject_unusable_frames([{"slide": "05:15", "section": "04:54"}], [obscured])
    assert out == []


def test_reject_unusable_keeps_unknown_labels(tmp_path):
    from forge_video_summarizer.stages.slides import _reject_unusable_frames
    pl = [{"slide": "09:99", "section": "04:54"}]
    assert _reject_unusable_frames(pl, []) == pl
