"""Configuration loaded from the project-local .env (and process env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .errors import ConfigError

__all__ = ["Config", "load_config"]


@dataclass
class Config:
    """Resolved runtime configuration."""

    bili_sessdata: str = ""
    speech_endpoint: str = ""
    speech_key: str = ""
    speech_languages: str = "zh-CN,en-US"
    openai_endpoint: str = ""
    openai_key: str = ""
    openai_deployment: str = "gpt-5.6-sol"

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


def load_config(env_path: str | os.PathLike | None = ".env") -> Config:
    """Build a Config from a .env file overlaid by process env (env wins)."""
    if env_path:
        load_dotenv(env_path, override=False)
    env = os.environ.get
    return Config(
        bili_sessdata=env("BILI_SESSDATA", ""),
        speech_endpoint=env("AZURE_SPEECH_ENDPOINT", ""),
        speech_key=env("AZURE_SPEECH_KEY", ""),
        speech_languages=env("AZURE_SPEECH_LANGUAGES", "zh-CN,en-US"),
        openai_endpoint=env("AZURE_OPENAI_ENDPOINT", ""),
        openai_key=env("AZURE_OPENAI_API_KEY", ""),
        openai_deployment=env("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-sol"),
    )
