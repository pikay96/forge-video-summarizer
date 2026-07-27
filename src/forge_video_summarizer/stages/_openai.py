"""Shared Azure OpenAI Responses API helpers (used by summarize + diagram stages)."""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..errors import SummarizationError


def make_client(config: Config):
    """Construct an OpenAI SDK client pointed at the configured Azure endpoint."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SummarizationError("The 'openai' package is required") from exc
    return OpenAI(base_url=config.openai_endpoint, api_key=config.openai_key)


def extract_output_text(response: Any) -> str:
    """Pull text out of a Responses API result across SDK shapes. Returns "" if none."""
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


def call_responses(client: Any, config: Config, instructions: str, user_input: str) -> str:
    """Run one Responses API call and return the extracted text.

    Any SDK/transport error is surfaced uniformly as SummarizationError.
    """
    try:
        response = client.responses.create(
            model=config.openai_deployment,
            instructions=instructions,
            input=user_input,
        )
    except Exception as exc:  # noqa: BLE001 - uniform surface for SDK/transport errors
        raise SummarizationError(f"OpenAI request failed: {exc}") from exc
    return extract_output_text(response)


def call_responses_vision(
    client: Any,
    config: Config,
    instructions: str,
    text: str,
    images: list[tuple[str, str]],
    *,
    timeout: float = 300.0,
) -> str:
    """Run one multimodal Responses call: a text part + N images.

    `images` is a list of (label, data_url) where data_url is a
    'data:image/...;base64,...' string. The label precedes each image so the model
    can refer to it (we use the slide's [MM:SS] timestamp as the label).
    Verified shape: input=[{role:user, content:[input_text, input_image, ...]}].

    A timeout is enforced: image payloads are large, and a hung request would otherwise
    stall the whole pipeline indefinitely.
    """
    content: list[dict] = [{"type": "input_text", "text": text}]
    for label, data_url in images:
        content.append({"type": "input_text", "text": label})
        content.append({"type": "input_image", "image_url": data_url})
    try:
        target = client.with_options(timeout=timeout) if hasattr(client, "with_options") \
            else client
        response = target.responses.create(
            model=config.openai_deployment,
            instructions=instructions,
            input=[{"role": "user", "content": content}],
        )
    except Exception as exc:  # noqa: BLE001 - uniform surface for SDK/transport errors
        raise SummarizationError(f"OpenAI vision request failed: {exc}") from exc
    return extract_output_text(response)
