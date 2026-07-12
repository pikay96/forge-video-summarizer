"""Pipeline orchestration: wire the four stages with file handoff + caching.

Each `run_*` method is idempotent given its inputs and honors the cache-or-skip
rule via the Workspace. `run_all` executes the full end-to-end pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .downloaders import get_downloader
from .errors import DownloadError
from .models import Transcript, VideoMetadata
from .stages import (
    export_summary,
    extract_audio,
    probe_duration,
    summarize_transcript,
    transcribe_audio,
)
from .stages.transcribe import parse_response
from .workspace import Workspace

__all__ = ["Pipeline"]

_LOCAL_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts"}


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


class Pipeline:
    def __init__(self, config: Config, output_root: str | Path = "output"):
        self.config = config
        self.output_root = Path(output_root)

    # ── Stage 1 ─────────────────────────────────────────────────────────
    def run_download(self, url: str, *, force: bool = False) -> Workspace:
        """Download a remote video into a per-video workspace."""
        downloader = get_downloader(url, self.config)
        metadata = downloader.fetch_metadata(url)
        ws = Workspace(self.output_root, metadata.title, metadata.video_id)
        ws.ensure()

        if ws.find_video() is not None and not force:
            self._write_metadata(ws, metadata)  # keep metadata fresh/cheap
            return ws

        result = downloader.download(url, ws.dir)
        self._write_metadata(ws, result.metadata)
        return ws

    def workspace_for_local(self, video_path: str | Path) -> Workspace:
        """Build a workspace for a local video file (skips download)."""
        video_path = Path(video_path)
        if not video_path.is_file():
            raise DownloadError(f"Local video not found: {video_path}")
        title = video_path.stem
        video_id = "local"
        ws = Workspace(self.output_root, title, video_id)
        ws.ensure()

        target = ws.video_path(video_path.suffix.lstrip(".") or "mp4")
        if not target.exists():
            target.write_bytes(video_path.read_bytes())

        duration = probe_duration(target)
        metadata = VideoMetadata(
            video_id=video_id,
            title=title,
            source_url=str(video_path),
            duration=duration,
        )
        self._write_metadata(ws, metadata)
        return ws

    # ── Stage 2 ─────────────────────────────────────────────────────────
    def run_extract(self, ws: Workspace, *, force: bool = False) -> Path:
        video = ws.find_video()
        if video is None:
            raise DownloadError(f"No video file in workspace: {ws.dir}")
        return extract_audio(video, ws.audio_path, force=force)

    # ── Stage 3 ─────────────────────────────────────────────────────────
    def run_transcribe(self, ws: Workspace, *, force: bool = False) -> Transcript:
        # transcript.json holds the RAW API response (source of truth). Re-parse it
        # on cache hits so the interpolated anchors are regenerated deterministically.
        if Workspace.should_skip(ws.transcript_json_path, force):
            raw = json.loads(ws.transcript_json_path.read_text("utf-8"))
            return parse_response(raw)

        duration = probe_duration(ws.audio_path)
        transcript = transcribe_audio(ws.audio_path, self.config, duration=duration)

        # Raw response = honest source of truth (real offsets only: 0 + total).
        ws.transcript_json_path.write_text(
            json.dumps(transcript.raw or transcript.to_dict(), ensure_ascii=False, indent=2),
            "utf-8",
        )
        # Human-readable anchors (interpolated, clearly labeled in the header).
        ws.transcript_txt_path.write_text(transcript.to_timestamped_text(), "utf-8")
        return transcript

    # ── Stage 4 ─────────────────────────────────────────────────────────
    def run_summarize(self, ws: Workspace, *, force: bool = False) -> Path:
        if Workspace.should_skip(ws.summary_path, force):
            return ws.summary_path

        transcript = parse_response(
            json.loads(ws.transcript_json_path.read_text("utf-8"))
        )
        metadata = self._read_metadata(ws)
        summary = summarize_transcript(transcript, self.config, metadata=metadata)
        ws.summary_path.write_text(summary, "utf-8")
        return ws.summary_path

    # ── Stage 5 ─────────────────────────────────────────────────────────
    def run_export(self, ws: Workspace, *, force: bool = False) -> str:
        """Export summary.md to a Notion subpage; return the page URL.
        Cached via notion_url.txt (skipped unless force)."""
        if Workspace.should_skip(ws.notion_url_path, force):
            return ws.notion_url_path.read_text("utf-8").strip()

        summary = ws.summary_path.read_text("utf-8")
        metadata = self._read_metadata(ws)
        url = export_summary(summary, self.config, metadata=metadata)
        ws.notion_url_path.write_text(url, "utf-8")
        return url

    # ── End-to-end ──────────────────────────────────────────────────────
    def run_all(self, source: str, *, force: bool = False, export: bool = False) -> Path:
        """Full pipeline: source (URL or local file) -> summary.md path.
        When export=True, also publishes to Notion (Stage 5)."""
        if _looks_like_url(source):
            ws = self.run_download(source, force=force)
        else:
            ws = self.workspace_for_local(source)
        self.run_extract(ws, force=force)
        self.run_transcribe(ws, force=force)
        summary_path = self.run_summarize(ws, force=force)
        if export:
            self.run_export(ws, force=force)
        return summary_path

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _write_metadata(ws: Workspace, metadata: VideoMetadata) -> None:
        ws.metadata_path.write_text(
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )

    @staticmethod
    def _read_metadata(ws: Workspace) -> VideoMetadata | None:
        if not ws.metadata_path.is_file():
            return None
        return VideoMetadata.from_dict(json.loads(ws.metadata_path.read_text("utf-8")))
