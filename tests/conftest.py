"""Shared fixtures."""

from __future__ import annotations

import pytest

from forge_video_summarizer.config import Config


@pytest.fixture
def config() -> Config:
    return Config(
        bili_sessdata="sess",
        speech_endpoint="https://speech.example.com/",
        speech_key="speech-key",
        speech_languages="zh-CN,en-US",
        openai_endpoint="https://oai.example.com/openai/v1",
        openai_key="oai-key",
        openai_deployment="gpt-5.6-sol",
    )
