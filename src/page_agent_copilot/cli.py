"""Command line: seed the store, serve the console, fold a
day's operations digest, or serve the MCP surface.

    page-agent-copilot seed
    page-agent-copilot serve [--port 8000]
    page-agent-copilot digest [--date 2026-10-18]
    page-agent-copilot mcp
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .config import Settings
from .service import CopilotService
from .store import build_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="page-agent-copilot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="seed OrderDesk data if the store is empty")
    serve = sub.add_parser("serve", help="run the console + API")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--host", default="127.0.0.1")
    digest = sub.add_parser(
        "digest",
        help="generate (or regenerate) one day's operations "
             "digest and print it")
    digest.add_argument(
        "--date", default=None,
        help="the day to summarise, YYYY-MM-DD (default: today, "
             "UTC)")
    sub.add_parser(
        "mcp",
        help="serve the MCP surface over stdio: machine clients "
             "submit tasks (planned + gated), never raw clicks")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.cmd == "seed":
        service = CopilotService(settings, build_store(settings))
        print("seeded" if service.seed() else "already seeded")
        return
    if args.cmd == "digest":
        service = CopilotService(settings, build_store(settings))
        day = args.date or datetime.now(timezone.utc).strftime(
            "%Y-%m-%d")
        print(json.dumps(service.generate_digest(day, "cli"),
                         indent=2))
        return
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("page_agent_copilot.api:app", host=args.host,
                    port=args.port)


if __name__ == "__main__":
    main()
