from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from forge_video_summarizer.stages import frames
from forge_video_summarizer.stages.frames import (
    SlideCandidate,
    _cluster,
    detect_slide_candidates,
    format_ts,
)


def test_format_ts():
    assert format_ts(6) == "00:06"
    assert format_ts(91) == "01:31"
    assert format_ts(3661) == "1:01:01"


def test_cluster_collapses_near_cuts_keeps_last():
    # two clusters: [1,3,5] (animation re-triggers) and [40,41]
    assert _cluster([1.0, 3.0, 5.0, 40.0, 41.0], min_gap=8.0) == [5.0, 41.0]


def test_cluster_empty():
    assert _cluster([], min_gap=8.0) == []


def test_detect_prepends_opening_slide_when_first_cut_late(tmp_path):
    # scene cuts start at 91s; expect a t=0 opening slide prepended
    def fake_pick(v, t, d):
        return Path(d) / f"slide_{format_ts(t).replace(':', '-')}.png"

    with (
        patch.object(frames, "_require_ffmpeg", lambda: None),
        patch.object(frames, "_scene_timestamps", return_value=[91.0, 120.0]),
        patch.object(frames, "_pick_clean_frame", side_effect=fake_pick),
    ):
        cands = detect_slide_candidates("video.mp4", tmp_path)
    tss = [c.timestamp for c in cands]
    assert tss[0] == 0.0
    assert 91.0 in tss and 120.0 in tss


def test_detect_skips_failed_frames(tmp_path):
    with (
        patch.object(frames, "_require_ffmpeg", lambda: None),
        patch.object(frames, "_scene_timestamps", return_value=[10.0, 50.0]),
        patch.object(frames, "_pick_clean_frame", return_value=None),
    ):
        cands = detect_slide_candidates("video.mp4", tmp_path)
    assert cands == []


def test_caption_score_no_pillow_returns_zero(tmp_path, monkeypatch):
    # simulate Pillow missing -> best-effort 0.0
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert frames._caption_score(tmp_path / "x.png") == 0.0


def test_pick_clean_frame_prefers_low_caption(tmp_path):
    # frame at t has caption; t+1.5 is clean -> picks the clean one
    def fake_extract(video, t, out):
        out.write_bytes(b"png")
        return True

    def fake_score(path):
        # encode the sampled time in the probe filename to vary score
        return 0.0 if "_probe_1" in path.name or "_probe_2" in path.name else 0.2

    with (
        patch.object(frames, "_extract_frame", side_effect=fake_extract),
        patch.object(frames, "_caption_score", side_effect=fake_score),
    ):
        out = frames._pick_clean_frame(Path("v.mp4"), 0.0, tmp_path)
    assert out is not None and out.name == "slide_00-00.png"


def test_slide_candidate_dataclass(tmp_path):
    c = SlideCandidate(timestamp=91.0, path=tmp_path / "s.png")
    assert c.timestamp == 91.0
