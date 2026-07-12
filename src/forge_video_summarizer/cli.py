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
    parser.add_argument("--env", default=".env", help="Path to .env (default: .env)")
    parser.add_argument("--output", default="output", help="Output root (default: output)")
    parser.add_argument("--force", action="store_true", help="Ignore cached artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in [
        ("summarize", "Run the full pipeline (download -> summary)"),
        ("download", "Stage 1: download a remote video"),
        ("extract", "Stage 2: extract audio"),
        ("transcribe", "Stage 3: transcribe"),
        ("summarize-transcript", "Stage 4: summarize an existing transcript"),
        ("export", "Stage 5: export an existing summary to Notion"),
    ]:
        arg = "url" if name == "download" else "source"
        p = sub.add_parser(name, help=help_text)
        p.add_argument(arg, help="bilibili URL or local video file path")
        if name == "summarize":
            p.add_argument(
                "--export", action="store_true", help="also publish to Notion (Stage 5)"
            )
    return parser


def _workspace_for(pipeline: Pipeline, source: str, *, force: bool):
    if _looks_like_url(source):
        return pipeline.run_download(source, force=force)
    return pipeline.workspace_for_local(source)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pipeline = Pipeline(load_config(args.env), output_root=args.output)
    force = args.force

    try:
        if args.command == "summarize":
            print(
                f"Summary written: {pipeline.run_all(args.source, force=force, export=args.export)}"
            )
            if args.export:
                ws = _workspace_for(pipeline, args.source, force=False)
                print(f"Notion page: {ws.notion_url_path.read_text('utf-8').strip()}")
        elif args.command == "download":
            print(f"Downloaded to: {pipeline.run_download(args.url, force=force).dir}")
        elif args.command == "extract":
            ws = _workspace_for(pipeline, args.source, force=force)
            print(f"Audio: {pipeline.run_extract(ws, force=force)}")
        elif args.command == "transcribe":
            ws = _workspace_for(pipeline, args.source, force=force)
            pipeline.run_extract(ws, force=force)
            pipeline.run_transcribe(ws, force=force)
            print(f"Transcript: {ws.transcript_txt_path}")
        elif args.command == "summarize-transcript":
            ws = _workspace_for(pipeline, args.source, force=force)
            print(f"Summary written: {pipeline.run_summarize(ws, force=force)}")
        elif args.command == "export":
            ws = _workspace_for(pipeline, args.source, force=force)
            print(f"Notion page: {pipeline.run_export(ws, force=force)}")
    except ForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
