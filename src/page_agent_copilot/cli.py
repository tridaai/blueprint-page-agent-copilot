"""Command line: seed the store, or serve the console.

    page-agent-copilot seed
    page-agent-copilot serve [--port 8000]
"""

from __future__ import annotations

import argparse

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
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.cmd == "seed":
        service = CopilotService(settings, build_store(settings))
        print("seeded" if service.seed() else "already seeded")
        return
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("page_agent_copilot.api:app", host=args.host,
                    port=args.port)


if __name__ == "__main__":
    main()
