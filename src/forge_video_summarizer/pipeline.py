"""Pipeline orchestration: wire the four stages with file handoff + caching.

Each `run_*` method is idempotent given its inputs and honors the cache-or-skip
rule via the Workspace. `run_all` executes the full end-to-end pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import Config
from .downloaders import get_downloader
from .errors import DownloadError
from .models import Transcript, VideoMetadata
from .stages import (
    clean_selected_frames,
    detect_slide_candidates,
    export_summary,
    extract_audio,
    generate_overview_image,
    place_slides,
    probe_duration,
    select_slide_placements,
    summarize_transcript,
    transcribe_audio,
)
from .stages.transcribe import parse_response
from .workspace import Workspace

__all__ = ["Pipeline"]

log = logging.getLogger(__name__)

_LOCAL_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts"}


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


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
    def run_summarize(
        self,
        ws: Workspace,
        *,
        force: bool = False,
        slides: bool = False,
        mask_overlays: bool = False,
    ) -> Path:
        if Workspace.should_skip(ws.summary_path, force):
            return ws.summary_path

        transcript = parse_response(
            json.loads(ws.transcript_json_path.read_text("utf-8"))
        )
        metadata = self._read_metadata(ws)
        summary = summarize_transcript(transcript, self.config, metadata=metadata)

        # Overview image (always-on): a dedicated second pass renders an Excalidraw scene
        # to overview.png, embedded at the top of the Notion page on export. Best-effort —
        # a failed render must never block the summary.
        try:
            generate_overview_image(summary, self.config, ws.overview_image_path)
        except Exception as exc:  # noqa: BLE001 - overview is additive, degrade gracefully
            log.warning("overview image step failed, continuing without it: %s", exc)

        # Slide screenshots (opt-in --slides): extract candidate slide frames, let the
        # vision model pick the key ones + assign each to a Walkthrough section, and insert
        # ![slide@MM:SS] placeholders into the summary. Best-effort — never blocks.
        if slides:
            try:
                summary = self._add_slides(ws, summary, mask_overlays=mask_overlays)
            except Exception as exc:  # noqa: BLE001 - slides are additive, degrade
                log.warning("slide step failed, continuing without slides: %s", exc)

        ws.summary_path.write_text(summary, "utf-8")
        return ws.summary_path

    def _add_slides(self, ws: Workspace, summary: str, *, mask_overlays: bool = False) -> str:
        """Detect slide frames -> model selects & places them -> summary with placeholders.

        Frames are embedded as captured. Overlay cleanup (webcam mask / chrome crop) is
        opt-in via `mask_overlays` and deliberately off by default: the core feature is
        getting the right screenshot into the summary, and cleanup is a separate polish
        step that can misjudge a region.
        """
        video = ws.find_video()
        if video is None:
            log.warning("no video file for slide extraction; skipping slides")
            return summary
        candidates = detect_slide_candidates(video, ws.slides_dir)
        if not candidates:
            return summary
        placements = select_slide_placements(candidates, summary, self.config)
        if not placements:
            return summary
        if mask_overlays:
            clean_selected_frames(candidates, placements)
        return place_slides(summary, placements)

    # ── Stage 5 ─────────────────────────────────────────────────────────
    def run_export(self, ws: Workspace, *, force: bool = False) -> str:
        """Export summary.md to a Notion subpage; return the page URL.
        Cached via notion_url.txt (skipped unless force)."""
        if Workspace.should_skip(ws.notion_url_path, force):
            return ws.notion_url_path.read_text("utf-8").strip()

        summary = ws.summary_path.read_text("utf-8")
        metadata = self._read_metadata(ws)
        overview = ws.overview_image_path if ws.overview_image_path.is_file() else None
        slides_dir = ws.slides_dir if ws.slides_dir.is_dir() else None
        url = export_summary(
            summary, self.config, metadata=metadata,
            overview_image=overview, slides_dir=slides_dir,
        )
        ws.notion_url_path.write_text(url, "utf-8")
        return url

    # ── End-to-end ──────────────────────────────────────────────────────
    def run_all(
        self,
        source: str,
        *,
        force: bool = False,
        export: bool = False,
        slides: bool = False,
        mask_overlays: bool = False,
    ) -> Path:
        """Full pipeline: source (URL or local file) -> summary.md path.
        When export=True, also publishes to Notion (Stage 5).
        When slides=True, extracts slide screenshots into the Walkthrough.
        When mask_overlays=True, also cleans webcam/chrome off those frames
        (experimental; off by default)."""
        if _looks_like_url(source):
            ws = self.run_download(source, force=force)
        else:
            ws = self.workspace_for_local(source)
        self.run_extract(ws, force=force)
        self.run_transcribe(ws, force=force)
        summary_path = self.run_summarize(
            ws, force=force, slides=slides, mask_overlays=mask_overlays
        )
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
