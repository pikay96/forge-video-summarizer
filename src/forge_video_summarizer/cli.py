"""Command-line interface: end-to-end `summarize` + four per-stage subcommands."""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .errors import ForgeError
from .pipeline import Pipeline, _looks_like_url

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge-video-summarizer",
        description="Turn a video (bilibili URL or local file) into a teacher-clear summary.",
    )
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument("--output", default="output", help="Output root dir (default: output)")
    parser.add_argument("--force", action="store_true", help="Ignore cached artifacts")

    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("summarize", help="Run the full pipeline (download->summary)")
    p_all.add_argument("source", help="bilibili URL or local video file path")

    p_dl = sub.add_parser("download", help="Stage 1: download a remote video")
    p_dl.add_argument("url", help="bilibili URL")

    p_ex = sub.add_parser("extract", help="Stage 2: extract audio from a workspace/video")
    p_ex.add_argument("source", help="bilibili URL or local video file path")

    p_tr = sub.add_parser("transcribe", help="Stage 3: transcribe an already-extracted workspace")
    p_tr.add_argument("source", help="bilibili URL or local video file path")

    p_sum = sub.add_parser("summarize-transcript", help="Stage 4: summarize an existing transcript")
    p_sum.add_argument("source", help="bilibili URL or local video file path")

    return parser


def _workspace_for(pipeline: Pipeline, source: str, *, force: bool):
    if _looks_like_url(source):
        return pipeline.run_download(source, force=force)
    return pipeline.workspace_for_local(source)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config(args.env)
    pipeline = Pipeline(config, output_root=args.output)

    try:
        if args.command == "summarize":
            out = pipeline.run_all(args.source, force=args.force)
            print(f"Summary written: {out}")

        elif args.command == "download":
            ws = pipeline.run_download(args.url, force=args.force)
            print(f"Downloaded to: {ws.dir}")

        elif args.command == "extract":
            ws = _workspace_for(pipeline, args.source, force=args.force)
            audio = pipeline.run_extract(ws, force=args.force)
            print(f"Audio: {audio}")

        elif args.command == "transcribe":
            ws = _workspace_for(pipeline, args.source, force=args.force)
            pipeline.run_extract(ws, force=args.force)
            pipeline.run_transcribe(ws, force=args.force)
            print(f"Transcript: {ws.transcript_txt_path}")

        elif args.command == "summarize-transcript":
            ws = _workspace_for(pipeline, args.source, force=args.force)
            out = pipeline.run_summarize(ws, force=args.force)
            print(f"Summary written: {out}")

    except ForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
