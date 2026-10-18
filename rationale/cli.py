"""Console entry points."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="rationale-guard")
    parser.add_argument("command", choices=["api", "mcp", "seed", "load"])
    args, rest = parser.parse_known_args()
    if args.command == "api":
        import uvicorn

        from rationale.api.main import app

        uvicorn.run(app, host="0.0.0.0", port=8000)
    elif args.command == "mcp":
        from rationale.mcp_server import main as mcp_main

        mcp_main()
    elif args.command == "seed":
        from rationale.ingest.__main__ import main as ingest_main

        ingest_main(["seed", *rest])
    else:
        from rationale.ingest.__main__ import main as ingest_main

        ingest_main(["load", *rest])
