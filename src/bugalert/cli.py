"""Local-first web server and a credential-safe replacement feed downloader."""

import argparse
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

import uvicorn

from bugalert.config import ConfigError, load_settings
from bugalert.feed import AristaClient, FeedError
from bugalert.server import MonitorServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Arista EOS bugs and security advisories.")
    parser.add_argument(
        "--config",
        default=os.getenv("BUGALERT_CONFIG", "config.toml"),
        help="External TOML file (default: config.toml or BUGALERT_CONFIG)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only)")
    parser.add_argument("--port", default=8000, type=int, help="HTTP port (default: 8000)")
    args = parser.parse_args()
    from bugalert.app import create_app

    try:
        app = create_app(args.config)
    except ConfigError as error:
        parser.error(str(error))
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    config = uvicorn.Config(
        app, host=args.host, port=args.port, workers=1, timeout_graceful_shutdown=5
    )
    server = MonitorServer(config, before_shutdown=lambda: app.state.monitor.begin_shutdown())
    # Match uvicorn.run(): Uvicorn replays Ctrl+C after graceful cleanup.
    with suppress(KeyboardInterrupt):
        server.run()


def fetch_main() -> None:
    parser = argparse.ArgumentParser(description="Download a strict JSON Arista bug feed.")
    parser.add_argument("--config", default=os.getenv("BUGALERT_CONFIG", "config.toml"))
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        feed = AristaClient().fetch(settings)
        text = json.dumps(feed.payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
    except (ConfigError, FeedError, OSError) as error:
        parser.exit(1, f"bugalert-fetch: {error}\n")
