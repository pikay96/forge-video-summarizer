from __future__ import annotations

import pytest

from forge_video_summarizer.models import (
    Transcript,
    TranscriptSegment,
    VideoMetadata,
    format_timestamp,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "[00:00]"),
        (5, "[00:05]"),
        (65, "[01:05]"),
        (599, "[09:59]"),
        (3600, "[01:00:00]"),
        (3661, "[01:01:01]"),
        (-3, "[00:00]"),
        (12.6, "[00:13]"),  # rounds
    ],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_segment_end():
    seg = TranscriptSegment(start=10.0, duration=2.5, text="hi")
    assert seg.end == 12.5


def test_transcript_duration_and_text():
    t = Transcript(
        segments=[
            TranscriptSegment(0.0, 2.0, "hello"),
            TranscriptSegment(65.0, 3.0, "world"),
        ],
        locale="en-US",
    )
    assert t.duration == 68.0
    txt = t.to_timestamped_text()
    assert txt.splitlines() == ["[00:00] hello", "[01:05] world"]


def test_transcript_empty_duration():
    assert Transcript().duration == 0.0


def test_transcript_roundtrip():
    t = Transcript(
        segments=[TranscriptSegment(1.0, 2.0, "a")],
        locale="zh-CN",
        full_text="a",
    )
    restored = Transcript.from_dict(t.to_dict())
    assert restored.locale == "zh-CN"
    assert restored.full_text == "a"
    assert restored.segments[0].text == "a"
    assert restored.segments[0].start == 1.0


def test_video_metadata_roundtrip_ignores_unknown_keys():
    meta = VideoMetadata(video_id="BV1", title="T", view_count=5)
    d = meta.to_dict()
    d["some_future_field"] = "x"
    restored = VideoMetadata.from_dict(d)
    assert restored.video_id == "BV1"
    assert restored.view_count == 5
