"""Pipeline stages, one module per stage."""

from __future__ import annotations

from .diagram import generate_overview_image
from .export_notion import export_summary
from .extract import extract_audio, probe_duration
from .frames import detect_slide_candidates
from .slides import place_slides, select_slide_placements
from .summarize import summarize_transcript
from .transcribe import transcribe_audio

__all__ = [
    "extract_audio",
    "probe_duration",
    "transcribe_audio",
    "summarize_transcript",
    "generate_overview_image",
    "detect_slide_candidates",
    "select_slide_placements",
    "place_slides",
    "export_summary",
]
