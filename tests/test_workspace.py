from __future__ import annotations

from forge_video_summarizer.workspace import Workspace, sanitize_title


def test_sanitize_replaces_invalid_chars():
    assert sanitize_title('a/b:c*d?"e<f>g|h') == "a_b_c_d__e_f_g_h"


def test_sanitize_collapses_whitespace_and_trims():
    assert sanitize_title("  hello   world  ") == "hello world"


def test_sanitize_strips_trailing_dots():
    assert sanitize_title("name...") == "name"


def test_sanitize_empty_becomes_untitled():
    assert sanitize_title("///") == "___"  # invalid chars become underscores
    assert sanitize_title("") == "untitled"
    assert sanitize_title("   ") == "untitled"


def test_sanitize_caps_length():
    long = "x" * 500
    assert len(sanitize_title(long)) <= 150


def test_workspace_dir_naming(tmp_path):
    ws = Workspace(tmp_path, "My Video", "BV123")
    assert ws.dir.name == "My Video[BV123]"


def test_workspace_dir_naming_no_id(tmp_path):
    ws = Workspace(tmp_path, "My Video", "")
    assert ws.dir.name == "My Video"


def test_workspace_artifact_paths(tmp_path):
    ws = Workspace(tmp_path, "T", "BV1")
    assert ws.audio_path.name == "audio.mp3"
    assert ws.transcript_json_path.name == "transcript.json"
    assert ws.transcript_txt_path.name == "transcript.txt"
    assert ws.summary_path.name == "summary.md"
    assert ws.metadata_path.name == "metadata.json"


def test_find_video(tmp_path):
    ws = Workspace(tmp_path, "T", "BV1")
    ws.ensure()
    assert ws.find_video() is None
    vid = ws.video_path("mp4")
    vid.write_text("x")
    assert ws.find_video() == vid


def test_should_skip(tmp_path):
    f = tmp_path / "a.txt"
    assert Workspace.should_skip(f, force=False) is False  # missing
    f.write_text("x")
    assert Workspace.should_skip(f, force=False) is True   # exists
    assert Workspace.should_skip(f, force=True) is False   # force overrides
