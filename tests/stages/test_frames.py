from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from forge_video_summarizer.stages import frames
from forge_video_summarizer.stages.frames import (
    SlideCandidate,
    _cluster,
    _fill_gaps,
    detect_slide_candidates,
    format_ts,
)


def test_format_ts():
    assert format_ts(6) == "00:06"
    assert format_ts(91) == "01:31"
    assert format_ts(3661) == "1:01:01"


def test_cluster_collapses_near_cuts_keeps_last():
    assert _cluster([1.0, 3.0, 5.0, 40.0, 41.0], min_gap=8.0) == [5.0, 41.0]


def test_cluster_empty():
    assert _cluster([], min_gap=8.0) == []


def test_fill_gaps_covers_long_blind_stretch():
    # the real bug: a 139s stretch with no candidate hid an important slide
    out = _fill_gaps([272.0, 411.0], duration=474.0, max_gap=45.0)
    between = [t for t in out if 272.0 < t < 411.0]
    assert between, "a long gap must be sampled"
    # no remaining gap exceeds the limit
    ordered = sorted(out)
    assert all(b - a <= 45.0 + 1e-6 for a, b in zip(ordered, ordered[1:], strict=False))


def test_fill_gaps_leaves_dense_timeline_alone():
    dense = [10.0, 30.0, 50.0]
    out = _fill_gaps(dense, duration=60.0, max_gap=45.0)
    for t in dense:
        assert t in out


def test_fill_gaps_drops_times_at_the_very_end():
    out = _fill_gaps([10.0], duration=20.0, max_gap=45.0)
    assert all(t < 19.0 for t in out)


def test_detect_uses_gap_fill_and_skips_failed_frames(tmp_path):
    def fake_pick(v, t, d):
        return Path(d) / f"slide_{format_ts(t).replace(':', '-')}.png"

    with (
        patch.object(frames, "_require_ffmpeg", lambda: None),
        patch.object(frames, "_probe_duration", return_value=474.0),
        patch.object(frames, "_scene_timestamps", return_value=[272.0, 411.0]),
        patch.object(frames, "_pick_clean_frame", side_effect=fake_pick),
    ):
        cands = detect_slide_candidates("video.mp4", tmp_path)
    tss = [c.timestamp for c in cands]
    assert any(272.0 < t < 411.0 for t in tss)  # blind stretch now covered


def test_detect_skips_failed_frames(tmp_path):
    with (
        patch.object(frames, "_require_ffmpeg", lambda: None),
        patch.object(frames, "_probe_duration", return_value=100.0),
        patch.object(frames, "_scene_timestamps", return_value=[10.0, 50.0]),
        patch.object(frames, "_pick_clean_frame", return_value=None),
    ):
        assert detect_slide_candidates("video.mp4", tmp_path) == []


def test_caption_score_no_pillow_returns_zero(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert frames._caption_score(tmp_path / "x.png") == 0.0


def test_pick_clean_frame_prefers_low_caption(tmp_path):
    def fake_extract(video, t, out):
        out.write_bytes(b"png")
        return True

    def fake_score(path):
        return 0.0 if "_probe_1" in path.name else 0.2

    with (
        patch.object(frames, "_extract_frame", side_effect=fake_extract),
        patch.object(frames, "_caption_score", side_effect=fake_score),
    ):
        out = frames._pick_clean_frame(Path("v.mp4"), 0.0, tmp_path)
    assert out is not None and out.name == "slide_00-00.png"


def test_slide_candidate_dataclass(tmp_path):
    c = SlideCandidate(timestamp=91.0, path=tmp_path / "s.png")
    assert c.timestamp == 91.0
