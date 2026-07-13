from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from forge_video_summarizer.stages.diagram import (
    build_overview_scene,
    extract_scene,
    generate_overview_image,
    render_excalidraw,
)

SCENE = {
    "type": "excalidraw",
    "version": 2,
    "elements": [
        {"type": "rectangle", "id": "r1", "x": 0, "y": 0, "width": 200, "height": 80},
    ],
    "appState": {"viewBackgroundColor": "#ffffff"},
}
SCENE_JSON = json.dumps(SCENE, ensure_ascii=False)


# ── extract_scene ───────────────────────────────────────────────────────────

def test_extract_bare_json():
    assert extract_scene(SCENE_JSON)["elements"][0]["id"] == "r1"


def test_extract_fenced_json():
    reply = f"Here:\n```json\n{SCENE_JSON}\n```"
    assert extract_scene(reply)["type"] == "excalidraw"


def test_extract_with_leading_prose():
    reply = f"Sure, here is the scene: {SCENE_JSON} — enjoy!"
    got = extract_scene(reply)
    assert got is not None and got["elements"]


def test_extract_rejects_non_scene():
    assert extract_scene('{"foo": 1}') is None  # no elements list
    assert extract_scene("not json at all") is None
    assert extract_scene('{"elements": "notalist"}') is None


# ── render_excalidraw (exporter mocked / faked) ─────────────────────────────

def test_render_missing_exporter(monkeypatch, tmp_path):
    monkeypatch.setattr("forge_video_summarizer.stages.diagram.find_exporter", lambda: None)
    ok, detail = render_excalidraw(dict(SCENE), tmp_path / "o.png")
    assert ok is False and detail == "no-exporter"


def test_render_success_writes_scene_and_checks_png(monkeypatch, tmp_path):
    out = tmp_path / "o.png"

    def fake_run(cmd, capture_output, text, timeout):
        # emulate the CLI writing the -o target
        o = cmd[cmd.index("-o") + 1]
        with open(o, "wb") as fh:
            fh.write(b"\x89PNG\r\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("forge_video_summarizer.stages.diagram.subprocess.run", fake_run)
    ok, detail = render_excalidraw(dict(SCENE), out, exporter="/fake/cli")
    assert ok is True and detail == "ok"
    assert out.exists()
    # scene JSON was written next to the png with envelope fields normalized
    scene_file = out.with_suffix(".excalidraw")
    assert scene_file.exists()
    written = json.loads(scene_file.read_text())
    assert written["type"] == "excalidraw" and written["appState"]["viewBackgroundColor"]


def test_render_failure_reports_error(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="error: bad scene")

    monkeypatch.setattr("forge_video_summarizer.stages.diagram.subprocess.run", fake_run)
    ok, detail = render_excalidraw(dict(SCENE), tmp_path / "o.png", exporter="/fake/cli")
    assert ok is False
    assert "bad scene" in detail


# ── build_overview_scene / generate_overview_image ──────────────────────────

def _client_returning(*texts):
    client = MagicMock()
    client.responses.create.side_effect = [
        SimpleNamespace(output_text=t) for t in texts
    ]
    return client


def test_build_scene_parses_model_reply(config):
    client = _client_returning(f"```json\n{SCENE_JSON}\n```")
    scene = build_overview_scene("# Summary", config, client=client)
    assert scene["elements"][0]["id"] == "r1"


def test_build_scene_empty_summary_returns_none(config):
    client = MagicMock()
    assert build_overview_scene("   ", config, client=client) is None
    client.responses.create.assert_not_called()


def test_generate_success_first_try(config, monkeypatch, tmp_path):
    def fake_render(scene, out, exporter=None):
        out.write_bytes(b"png")
        return (True, "ok")

    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.render_excalidraw", fake_render
    )
    client = _client_returning(SCENE_JSON)
    out = tmp_path / "overview.png"
    got = generate_overview_image("# Summary", config, out, client=client)
    assert got == out
    assert client.responses.create.call_count == 1


def test_generate_retries_on_bad_scene_then_succeeds(config, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.render_excalidraw",
        lambda scene, out, exporter=None: (True, "ok"),
    )
    # first reply is not a scene, second is valid
    client = _client_returning("no scene here", SCENE_JSON)
    out = tmp_path / "overview.png"
    got = generate_overview_image("# Summary", config, out, client=client)
    assert got == out
    assert client.responses.create.call_count == 2


def test_generate_gives_up_when_exporter_absent(config, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.render_excalidraw",
        lambda scene, out, exporter=None: (False, "no-exporter"),
    )
    client = _client_returning(SCENE_JSON)
    out = tmp_path / "overview.png"
    got = generate_overview_image("# Summary", config, out, client=client)
    assert got is None
    # no retry when the tool is simply missing
    assert client.responses.create.call_count == 1


def test_generate_gives_up_after_retry(config, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.render_excalidraw",
        lambda scene, out, exporter=None: (False, "render error"),
    )
    client = _client_returning(SCENE_JSON, SCENE_JSON)
    out = tmp_path / "overview.png"
    got = generate_overview_image("# Summary", config, out, client=client)
    assert got is None
    assert client.responses.create.call_count == 2


def test_generate_empty_summary_returns_none(config):
    client = MagicMock()
    assert generate_overview_image("  ", config, __import__("pathlib").Path("/x.png"),
                                   client=client) is None
    client.responses.create.assert_not_called()
