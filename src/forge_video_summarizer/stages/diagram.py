"""Overview image — an Excalidraw diagram rendered to PNG from the finished summary.

A dedicated *second pass* (after Stage 4 summarization): the model reads the finished
summary and produces ONE Excalidraw scene (structured JSON) that gives an at-a-glance visual
overview of the topic. We render that JSON to a PNG with `excalidraw-brute-export-cli` and
embed the image at the top of the Notion page.

Why Excalidraw (not Mermaid, not image-gen):
- Mermaid's auto-layout came out cramped and hard to read; Excalidraw gives clean,
  deliberately-placed boxes and arrows (rendered in a normal, non hand-drawn font).
- It's still STRUCTURED (real labels, correct relationships) — not a garbled image-gen
  picture. The model authors the scene JSON; the labels are real content from the summary.
- The output is a normal PNG that embeds anywhere (Notion image block).

Rendering needs the Node CLI `excalidraw-brute-export-cli`, which drives a headless browser
against excalidraw.com. If the CLI/browser is unavailable, the step degrades gracefully — the
summary always ships, just without the overview image.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..config import Config
from ._openai import call_responses, make_client

__all__ = [
    "generate_overview_image",
    "build_overview_scene",
    "render_excalidraw",
    "extract_scene",
    "find_exporter",
]

log = logging.getLogger(__name__)

_EXPORTER_BIN = "excalidraw-brute-export-cli"

# Excalidraw font families: 1 = Virgil (hand-drawn), 2 = Helvetica (normal), 3 = Cascadia.
# Pikay wants a normal, non hand-drawn font.
_NORMAL_FONT_FAMILY = 2


def _load_specialist() -> str:
    """Load the vendored excalidraw specialist prompt shipped as package data."""
    try:
        res = files("forge_video_summarizer.prompts").joinpath("excalidraw-specialist.md")
        return res.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - best-effort context; degrade to no-reference
        log.warning("excalidraw specialist prompt not found; proceeding without it")
        return ""


_SCENE_INSTRUCTION = """\
You produce ONE Excalidraw scene that is a clear, friendly VISUAL OVERVIEW of a written
summary of a video. Think "one-slide concept map": the reader should grasp the whole topic
and how its parts relate at a glance.

Output rules:
- Output ONLY a single JSON object — a valid Excalidraw file: {"type":"excalidraw",
  "version":2,"source":"forge-video-summarizer","elements":[...],"appState":
  {"viewBackgroundColor":"#ffffff"}}. No prose, no markdown fences, nothing else.
- Use real content from the summary for every label — accurate concepts and relationships,
  never invented placeholders.
- Text labels (inside shapes and on arrows) MUST be in the summary's dominant language
  (match the summary's language).
- Keep it clean and READABLE: a handful of well-spaced boxes (aim ~4–9), grouped logically,
  connected with labeled arrows. Prefer fewer, larger, well-separated elements over many
  tiny cramped ones. Leave generous gaps (>= 40px) so nothing overlaps.
- Use the container-binding approach for labeled shapes (a shape with boundElements + a text
  element with containerId) — never a bare "label" property. Use fontFamily:2 (a normal, NON
  hand-drawn font) for ALL text, fontSize >= 20 for shape labels and titles, >= 16 for arrow
  labels. Use the color palette for meaning.

Below is the Excalidraw element-format specialist reference. Follow it exactly for valid JSON
(required fields, container binding, arrow bindings, drawing order, sizing, colors).

────────────────────────────────────────────────────────────────────────────
__SPECIALIST__
────────────────────────────────────────────────────────────────────────────
"""


def extract_scene(text: str) -> dict | None:
    """Parse an Excalidraw scene object out of a model reply.

    Accepts a bare JSON object or one wrapped in a ```json / ``` fence. Returns the parsed
    dict if it looks like an Excalidraw scene (has an "elements" list), else None.
    """
    candidate = text.strip()
    # Strip a fenced code block if present.
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    # Fall back to slicing the outermost braces if there's stray prose.
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        scene = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(scene, dict) or not isinstance(scene.get("elements"), list):
        return None
    return scene


def _normalize_scene(scene: dict) -> dict:
    """Ensure the scene has the envelope fields the renderer expects, and force a normal
    (non hand-drawn) font on every text element so the image reads cleanly."""
    scene.setdefault("type", "excalidraw")
    scene.setdefault("version", 2)
    scene.setdefault("source", "forge-video-summarizer")
    appstate = scene.setdefault("appState", {})
    appstate.setdefault("viewBackgroundColor", "#ffffff")
    appstate.setdefault("currentItemFontFamily", _NORMAL_FONT_FAMILY)
    for el in scene.get("elements") or []:
        if isinstance(el, dict) and el.get("type") == "text":
            el["fontFamily"] = _NORMAL_FONT_FAMILY  # 2 = Helvetica; never 1 (Virgil/hand-drawn)
    return scene


def find_exporter() -> str | None:
    """Locate the excalidraw exporter: project-local node_modules first, then PATH."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "node_modules" / ".bin" / _EXPORTER_BIN
        if candidate.is_file():
            return str(candidate)
        if (parent / "pyproject.toml").is_file():
            break
    return shutil.which(_EXPORTER_BIN)


def render_excalidraw(
    scene: dict,
    out_png: Path,
    *,
    scene_path: Path | None = None,
    exporter: str | None = None,
    scale: int = 2,
) -> tuple[bool, str]:
    """Render an Excalidraw scene dict to a PNG via the export CLI.

    Writes the scene JSON (to scene_path if given, else alongside out_png), invokes the CLI,
    and checks the PNG was produced. Returns (ok, detail). Missing CLI -> (False, "no-exporter")
    so the caller can degrade; the pipeline is never failed for a missing optional tool.
    """
    binary = exporter or find_exporter()
    if not binary:
        log.warning("%s not found; skipping overview image render", _EXPORTER_BIN)
        return False, "no-exporter"

    scene = _normalize_scene(scene)
    src = scene_path or out_png.with_suffix(".excalidraw")
    src.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")

    try:
        proc = subprocess.run(
            [binary, "-i", str(src), "-o", str(out_png), "-f", "png", "-s", str(scale),
             "-b", "true"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"exporter invocation failed: {exc}"

    if proc.returncode == 0 and out_png.exists() and out_png.stat().st_size > 0:
        return True, "ok"
    detail = (proc.stderr or proc.stdout or "").strip()
    for line in detail.splitlines():
        if "error" in line.lower():
            detail = line.strip()
            break
    return False, detail or f"exporter exit {proc.returncode}"


def _scene_from_model(config: Config, client: Any, user_input: str) -> dict | None:
    """One model call: build the instruction, send user_input, parse the reply into a scene."""
    instruction = _SCENE_INSTRUCTION.replace("__SPECIALIST__", _load_specialist())
    return extract_scene(call_responses(client, config, instruction, user_input))


_SCENE_REQUEST = "Here is the finished summary. Produce the Excalidraw overview scene.\n\n"
_SCENE_RETRY_NUDGE = (
    "\n\nYour previous attempt did not produce a renderable Excalidraw scene. Return ONLY a "
    "single valid Excalidraw JSON object (type/version/elements/appState), with every "
    "labeled shape using the boundElements + containerId text binding, and no prose or "
    "code fences."
)


def build_overview_scene(
    summary_markdown: str,
    config: Config,
    *,
    client: Any | None = None,
) -> dict | None:
    """Ask the model for an Excalidraw scene (parsed dict) for a summary, or None."""
    config.require_openai()
    if not summary_markdown.strip():
        return None
    client = client or make_client(config)
    return _scene_from_model(config, client, _SCENE_REQUEST + summary_markdown)


def generate_overview_image(
    summary_markdown: str,
    config: Config,
    out_png: Path,
    *,
    client: Any | None = None,
    exporter: str | None = None,
) -> Path | None:
    """Generate + render an Excalidraw overview image to out_png. Returns the path or None.

    Flow: model authors the scene JSON -> parse -> render to PNG. On invalid JSON or a failed
    render, regenerate once (with a corrective nudge); if that also fails, return None so the
    caller degrades (the summary ships without an overview image).
    """
    config.require_openai()
    if not summary_markdown.strip():
        return None
    client = client or make_client(config)

    inputs = (
        _SCENE_REQUEST + summary_markdown,
        _SCENE_REQUEST + summary_markdown + _SCENE_RETRY_NUDGE,
    )
    for attempt, user_input in enumerate(inputs):
        scene = _scene_from_model(config, client, user_input)
        if scene is None:
            log.warning("no valid Excalidraw scene from model (attempt %d)", attempt + 1)
            continue
        ok, detail = render_excalidraw(scene, out_png, exporter=exporter)
        if ok:
            return out_png
        if detail == "no-exporter":
            return None  # tool absent — retrying won't help
        log.warning("overview image render failed (attempt %d): %s", attempt + 1, detail)
    return None
