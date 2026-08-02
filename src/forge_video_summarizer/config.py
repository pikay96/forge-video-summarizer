"""Configuration loaded from the project-local .env (and process env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from .errors import ConfigError

__all__ = ["Config", "load_config", "normalize_page_id"]

_HEX32 = re.compile(r"([0-9a-fA-F]{32})")


def normalize_page_id(value: str) -> str:
    """Extract a Notion page id (dashed UUID) from a raw id, UUID, or full URL.

    Accepts `39b0...f46e`, `39b0ddcb-...-f46e`, or a Notion share URL; returns the
    canonical 8-4-4-4-12 dashed UUID. Returns "" for empty input.
    """
    if not value:
        return ""
    m = _HEX32.search(value.replace("-", ""))
    if not m:
        return value.strip()
    h = m.group(1).lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


@dataclass
class Config:
    """Resolved runtime configuration."""

    bili_sessdata: str = ""
    xhs_cookie: str = ""
    douyin_cookie: str = ""
    speech_endpoint: str = ""
    speech_key: str = ""
    speech_model: str = "mai-transcribe-1.5"
    openai_endpoint: str = ""
    openai_key: str = ""
    openai_deployment: str = "gpt-5.6-sol"
    notion_key: str = ""
    notion_parent_page_id: str = ""

    def require_speech(self) -> None:
        if not (self.speech_endpoint and self.speech_key):
            raise ConfigError(
                "Azure Speech not configured: set AZURE_SPEECH_ENDPOINT and "
                "AZURE_SPEECH_KEY in your .env."
            )

    def require_openai(self) -> None:
        if not (self.openai_endpoint and self.openai_key):
            raise ConfigError(
                "Azure OpenAI not configured: set AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_API_KEY in your .env."
            )

    def require_notion(self) -> None:
        if not (self.notion_key and self.notion_parent_page_id):
            raise ConfigError(
                "Notion not configured: set NOTION_API_KEY and "
                "NOTION_PARENT_PAGE_ID in your .env."
            )

    @property
    def notion_parent_id(self) -> str:
        """Normalize the parent page id from a raw id, dashed UUID, or full URL."""
        return normalize_page_id(self.notion_parent_page_id)


def load_config(env_path: str | os.PathLike | None = ".env") -> Config:
    """Build a Config from a .env file overlaid by process env (env wins)."""
    if env_path:
        load_dotenv(env_path, override=False)
    env = os.environ.get
    return Config(
        bili_sessdata=env("BILI_SESSDATA", ""),
        xhs_cookie=env("XHS_COOKIE", ""),
        douyin_cookie=env("DOUYIN_COOKIE", ""),
        speech_endpoint=env("AZURE_SPEECH_ENDPOINT", ""),
        speech_key=env("AZURE_SPEECH_KEY", ""),
        speech_model=env("AZURE_SPEECH_MODEL", "mai-transcribe-1.5"),
        openai_endpoint=env("AZURE_OPENAI_ENDPOINT", ""),
        openai_key=env("AZURE_OPENAI_API_KEY", ""),
        openai_deployment=env("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-sol"),
        notion_key=env("NOTION_API_KEY", ""),
        notion_parent_page_id=env("NOTION_PARENT_PAGE_ID", ""),
    )
