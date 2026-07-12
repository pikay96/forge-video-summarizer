"""Configuration loaded from the project-local .env (and process env).

A tiny dependency-free .env parser keeps the project lean (no python-dotenv).
Process environment variables take precedence over .env file values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

__all__ = ["Config", "load_env_file", "load_config"]


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores blanks/comments. No interpolation."""
    result: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return result
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


@dataclass
class Config:
    """Resolved runtime configuration."""

    # Bilibili
    bili_sessdata: str = ""

    # Azure Speech (transcription)
    speech_endpoint: str = ""
    speech_key: str = ""
    speech_model: str = "mai-transcribe-1.5"

    # Azure OpenAI (summarization)
    openai_endpoint: str = ""
    openai_key: str = ""
    openai_deployment: str = "gpt-5.6-sol"

    def require_speech(self) -> None:
        if not self.speech_endpoint or not self.speech_key:
            raise ConfigError(
                "Azure Speech not configured: set AZURE_SPEECH_ENDPOINT and "
                "AZURE_SPEECH_KEY in your .env."
            )

    def require_openai(self) -> None:
        if not self.openai_endpoint or not self.openai_key:
            raise ConfigError(
                "Azure OpenAI not configured: set AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_API_KEY in your .env."
            )


def load_config(env_path: str | Path | None = ".env") -> Config:
    """Build a Config from a .env file overlaid by process environment."""
    file_values = load_env_file(env_path) if env_path else {}

    def get(key: str, default: str = "") -> str:
        # Process env wins over the .env file.
        return os.environ.get(key) or file_values.get(key) or default

    return Config(
        bili_sessdata=get("BILI_SESSDATA"),
        speech_endpoint=get("AZURE_SPEECH_ENDPOINT"),
        speech_key=get("AZURE_SPEECH_KEY"),
        speech_model=get("AZURE_SPEECH_MODEL", "mai-transcribe-1.5"),
        openai_endpoint=get("AZURE_OPENAI_ENDPOINT"),
        openai_key=get("AZURE_OPENAI_API_KEY"),
        openai_deployment=get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-sol"),
    )
