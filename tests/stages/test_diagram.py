from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from forge_video_summarizer.stages.diagram import (
    OVERVIEW_HEADING,
    extract_mermaid,
    generate_overview_diagram,
    prepend_overview,
    validate_mermaid,
)

VALID = "flowchart TD\n    A[开始] --> B[结束]"


# ── extract_mermaid ─────────────────────────────────────────────────────────

def test_extract_from_fenced_block():
    reply = f"Here you go:\n```mermaid\n{VALID}\n```\nDone."
    assert extract_mermaid(reply) == VALID


def test_extract_from_bare_diagram():
    # no fences, but starts with a known diagram type
    assert extract_mermaid(VALID) == VALID


def test_extract_returns_empty_when_none():
    assert extract_mermaid("just some prose, no diagram here") == ""


def test_extract_prefers_fenced_over_bare():
    reply = f"```mermaid\n{VALID}\n```"
    assert extract_mermaid(reply) == VALID


# ── validate_mermaid (mmdc mocked) ──────────────────────────────────────────

def test_validate_empty_is_invalid():
    ok, detail = validate_mermaid("   ")
    assert ok is False
    assert "empty" in detail


def test_validate_missing_mmdc_degrades_to_unvalidated(monkeypatch):
    # find_mmdc returns None -> we do NOT fail the pipeline
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.find_mmdc", lambda: None
    )
    ok, detail = validate_mermaid(VALID)
    assert ok is True
    assert detail == "unvalidated"


def test_validate_success(monkeypatch, tmp_path):
    # Fake mmdc that "renders" by writing the output file and exiting 0.
    def fake_run(cmd, capture_output, text, timeout):
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as fh:
            fh.write("<svg/>")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("forge_video_summarizer.stages.diagram.subprocess.run", fake_run)
    ok, detail = validate_mermaid(VALID, mmdc="/fake/mmdc")
    assert ok is True
    assert detail == "ok"


def test_validate_parse_error(monkeypatch):
    # mmdc exits 1, writes no output, emits a parse error line.
    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=1, stdout="", stderr="Error: Parse error on line 2:\n  ...",
        )

    monkeypatch.setattr("forge_video_summarizer.stages.diagram.subprocess.run", fake_run)
    ok, detail = validate_mermaid("flowchart TD\n  A[bad", mmdc="/fake/mmdc")
    assert ok is False
    assert "Parse error" in detail


def test_validate_invokes_real_subprocess(tmp_path):
    # A fake mmdc executable that mimics the real contract: `-o <out>` file written on
    # success. Exercises the actual subprocess invocation path (arg parsing, file check).
    fake = tmp_path / "mmdc"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'out=""\n'
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { out="$2"; shift; }; shift; done\n'
        'echo "<svg/>" > "$out"\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    ok, detail = validate_mermaid(VALID, mmdc=str(fake))
    assert ok is True and detail == "ok"


def test_validate_real_subprocess_rejects(tmp_path):
    # Fake mmdc that exits 1 and writes nothing -> invalid.
    fake = tmp_path / "mmdc"
    fake.write_text("#!/usr/bin/env bash\necho 'Parse error on line 1' >&2\nexit 1\n")
    fake.chmod(0o755)
    ok, detail = validate_mermaid("flowchart TD\n A[", mmdc=str(fake))
    assert ok is False
    assert "Parse error" in detail


# ── generate_overview_diagram (model + validation mocked) ───────────────────

def _client_returning(*texts):
    """A fake OpenAI client whose responses.create yields the given texts in order."""
    client = MagicMock()
    client.responses.create.side_effect = [
        SimpleNamespace(output_text=t) for t in texts
    ]
    return client


def test_generate_success_first_try(config, monkeypatch):
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.validate_mermaid",
        lambda code, mmdc=None: (True, "ok"),
    )
    client = _client_returning(f"```mermaid\n{VALID}\n```")
    out = generate_overview_diagram("# Summary\n\nbody", config, client=client)
    assert out == VALID
    assert client.responses.create.call_count == 1


def test_generate_retries_on_invalid_then_succeeds(config, monkeypatch):
    calls = {"n": 0}

    def fake_validate(code, mmdc=None):
        calls["n"] += 1
        return (calls["n"] > 1, "Parse error" if calls["n"] == 1 else "ok")

    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.validate_mermaid", fake_validate
    )
    client = _client_returning(
        "```mermaid\nflowchart TD\n  A[bad\n```",  # invalid first
        f"```mermaid\n{VALID}\n```",  # fixed on retry
    )
    out = generate_overview_diagram("# Summary", config, client=client)
    assert out == VALID
    assert client.responses.create.call_count == 2


def test_generate_gives_up_after_retry(config, monkeypatch):
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.validate_mermaid",
        lambda code, mmdc=None: (False, "Parse error"),
    )
    client = _client_returning(
        "```mermaid\nbad1\n```",
        "```mermaid\nbad2\n```",
    )
    out = generate_overview_diagram("# Summary", config, client=client)
    assert out is None
    assert client.responses.create.call_count == 2


def test_generate_empty_summary_returns_none(config):
    client = MagicMock()
    assert generate_overview_diagram("   ", config, client=client) is None
    client.responses.create.assert_not_called()


def test_generate_handles_output_blocks_shape(config, monkeypatch):
    # Response without output_text, using the nested output/content/text block shape.
    monkeypatch.setattr(
        "forge_video_summarizer.stages.diagram.validate_mermaid",
        lambda code, mmdc=None: (True, "ok"),
    )
    block = SimpleNamespace(text=f"```mermaid\n{VALID}\n```")
    item = SimpleNamespace(content=[block])
    resp = SimpleNamespace(output_text=None, output=[item])
    client = MagicMock()
    client.responses.create.return_value = resp
    out = generate_overview_diagram("# Summary", config, client=client)
    assert out == VALID


# ── prepend_overview ────────────────────────────────────────────────────────

def test_prepend_overview_adds_section():
    out = prepend_overview("# Title\n\nbody", VALID)
    assert out.startswith(OVERVIEW_HEADING)
    assert "```mermaid" in out
    assert VALID in out
    assert out.rstrip().endswith("body")
