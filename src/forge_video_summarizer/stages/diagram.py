"""Overview diagram — a Mermaid diagram generated from the finished summary.

A dedicated *second pass* (after Stage 4 summarization): the model reads the finished
summary and produces ONE Mermaid overview diagram capturing the structure/key concepts.
Rationale (see docs/PRD-overview-diagram.md):
- Mermaid (structured code) over image-gen: faithful labels + real relationships, not a
  plausible-but-garbled picture.
- Second pass over inline: the model gives the diagram undivided attention, works from the
  already-distilled summary, and the specialist instruction becomes the whole system prompt.
- Diagram type is the model's choice, guided by the vendored mermaid specialist skill.

Validation is STRICT: the generated Mermaid is actually parsed by `@mermaid-js/mermaid-cli`
(`mmdc`). On failure we regenerate once (feeding the parser error back), then degrade
gracefully — the summary always ships, at worst with an unrendered code block.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import SummarizationError

__all__ = [
    "generate_overview_diagram",
    "prepend_overview",
    "validate_mermaid",
    "extract_mermaid",
    "find_mmdc",
    "OVERVIEW_HEADING",
]

log = logging.getLogger(__name__)

OVERVIEW_HEADING = "## Overview"

_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
# Known Mermaid diagram headers — a generated block must start with one of these.
_KNOWN_DIAGRAM_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
    "erDiagram", "journey", "gantt", "pie", "mindmap", "timeline", "gitGraph",
    "C4Context", "C4Container", "C4Component", "quadrantChart", "requirementDiagram",
    "sankey", "xychart", "block-beta",
)

_DIAGRAM_INSTRUCTION = """\
You produce ONE Mermaid overview diagram for a written summary of a video.

Goal: an at-a-glance MAP of how the material's key concepts/steps relate — the kind of
diagram that helps a reader grasp and remember the whole before reading the prose.

Rules:
- Output ONLY a single fenced ```mermaid code block. No prose before or after it.
- Choose the diagram TYPE that best fits the content (flowchart for processes/decisions,
  mindmap for concept-heavy material, sequenceDiagram for interactions, stateDiagram for
  lifecycles, etc.) per the specialist guidance below.
- Keep it focused and legible: aim for well under ~20 nodes; group with subgraphs if useful.
- Node LABELS must be in the summary's dominant language (match the summary's language).
- Labels must be REAL content from the summary — accurate concepts and relationships, never
  invented placeholders.
- Keep labels short. Avoid characters that break Mermaid parsing inside labels: prefer plain
  text; if you need punctuation, wrap the label text in double quotes.

Below is a Mermaid specialist reference. Use it for correct syntax and type selection.

────────────────────────────────────────────────────────────────────────────
{specialist}
────────────────────────────────────────────────────────────────────────────
"""


def _load_specialist() -> str:
    """Load the vendored mermaid-diagram-specialist prompt shipped as package data."""
    try:
        res = files("forge_video_summarizer.prompts").joinpath(
            "mermaid-diagram-specialist.md"
        )
        return res.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - prompt is best-effort context; degrade to no-ref
        log.warning("mermaid specialist prompt not found; proceeding without it")
        return ""


def extract_mermaid(text: str) -> str:
    """Pull the Mermaid code out of a model reply.

    Accepts a fenced ```mermaid block (preferred) or, failing that, a bare body that
    already starts with a known diagram type. Returns the inner code (no fences), stripped.
    """
    m = _MERMAID_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    stripped = text.strip()
    # Sometimes the model returns the raw diagram without fences.
    if stripped.startswith(_KNOWN_DIAGRAM_TYPES):
        return stripped
    return ""


def find_mmdc() -> str | None:
    """Locate the mermaid-cli binary: project-local node_modules first, then PATH."""
    # Walk up from this file to find a node_modules/.bin/mmdc (project root).
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "node_modules" / ".bin" / "mmdc"
        if candidate.is_file():
            return str(candidate)
        if (parent / "pyproject.toml").is_file():
            break  # reached project root; stop climbing
    return shutil.which("mmdc")


def validate_mermaid(code: str, *, mmdc: str | None = None) -> tuple[bool, str]:
    """Strictly validate Mermaid by rendering it with mmdc.

    Returns (is_valid, detail). If mmdc is unavailable, returns (True, "unvalidated") — we
    do NOT fail the pipeline for a missing optional Node tool; the block still renders in
    Notion when the model got it right.
    """
    if not code.strip():
        return False, "empty diagram"

    binary = mmdc or find_mmdc()
    if not binary:
        log.warning("mmdc (mermaid-cli) not found; skipping strict diagram validation")
        return True, "unvalidated"

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "diagram.mmd"
        out = Path(td) / "diagram.svg"
        src.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [binary, "-i", str(src), "-o", str(out)],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"mmdc invocation failed: {exc}"
        # Valid iff mmdc exited 0 AND produced the output file.
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True, "ok"
        detail = (proc.stderr or proc.stdout or "").strip()
        # Surface just the salient parse-error line to feed back to the model.
        for line in detail.splitlines():
            if "error" in line.lower():
                detail = line.strip()
                break
        return False, detail or f"mmdc exit {proc.returncode}"


def _make_client(config: Config):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SummarizationError("The 'openai' package is required for diagrams") from exc
    return OpenAI(base_url=config.openai_endpoint, api_key=config.openai_key)


def _extract_output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text.strip()
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        for block in content or []:
            block_text = getattr(block, "text", None)
            if block_text is None and isinstance(block, dict):
                block_text = block.get("text")
            if block_text:
                chunks.append(block_text)
    return "".join(chunks).strip()


def _call_model(client: Any, config: Config, instruction: str, user_input: str) -> str:
    try:
        response = client.responses.create(
            model=config.openai_deployment,
            instructions=instruction,
            input=user_input,
        )
    except Exception as exc:  # noqa: BLE001 - uniform surface for SDK/transport errors
        raise SummarizationError(f"diagram request failed: {exc}") from exc
    return _extract_output_text(response)


def generate_overview_diagram(
    summary_markdown: str,
    config: Config,
    *,
    client: Any | None = None,
    mmdc: str | None = None,
) -> str | None:
    """Generate a validated Mermaid overview diagram (inner code, no fences) for a summary.

    Returns the Mermaid code on success (validated, or unvalidated if mmdc is absent), or
    None if generation/validation fails after one retry — callers degrade gracefully.
    """
    config.require_openai()
    if not summary_markdown.strip():
        return None

    client = client or _make_client(config)
    instruction = _DIAGRAM_INSTRUCTION.format(specialist=_load_specialist())
    user_input = (
        f"Here is the finished summary. Produce the overview diagram.\n\n{summary_markdown}"
    )

    reply = _call_model(client, config, instruction, user_input)
    code = extract_mermaid(reply)
    ok, detail = validate_mermaid(code, mmdc=mmdc)
    if ok:
        return code

    # One retry: feed the invalid diagram + parser error back and ask for a fix.
    log.warning("overview diagram invalid (%s); regenerating once", detail)
    retry_input = (
        f"{user_input}\n\nYour previous Mermaid diagram was INVALID and failed to render.\n"
        f"Renderer error: {detail}\n"
        f"Previous diagram:\n```mermaid\n{code}\n```\n"
        "Return a corrected single ```mermaid block that renders cleanly."
    )
    reply = _call_model(client, config, instruction, retry_input)
    code = extract_mermaid(reply)
    ok, detail = validate_mermaid(code, mmdc=mmdc)
    if ok:
        return code

    log.warning("overview diagram still invalid after retry (%s); skipping", detail)
    return None


def prepend_overview(summary_markdown: str, mermaid_code: str) -> str:
    """Prepend an `## Overview` Mermaid section to a summary (idempotent-ish: only adds)."""
    block = f"{OVERVIEW_HEADING}\n\n```mermaid\n{mermaid_code}\n```\n\n"
    return block + summary_markdown
