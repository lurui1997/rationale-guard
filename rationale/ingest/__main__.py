"""python -m rationale.ingest seed|load|pr"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rationale.db import Store, graph_name_for_repo
from rationale.ingest.load import load_json, load_snapshot
from rationale.ingest.pipeline import snapshot_from_pr, snapshot_from_seeds
from rationale.settings import repo_root


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rationale.ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Fetch the configured public repo and write data/seed JSON")
    seed.add_argument("--mode", choices=["auto", "offline", "llm"], default="offline")
    seed.add_argument("--out", default=str(repo_root() / "data" / "seed" / "paperclip.json"))

    load = sub.add_parser("load", help="Load a snapshot JSON into FalkorDB")
    load.add_argument("path", nargs="?", default=str(repo_root() / "data" / "seed" / "paperclip.json"))
    load.add_argument("--graph", default=None)
    load.add_argument("--reset", action="store_true")

    pr = sub.add_parser("pr", help="Ingest one public PR URL into its own graph")
    pr.add_argument("url")
    pr.add_argument("--mode", choices=["auto", "offline", "llm"], default="auto")
    pr.add_argument("--out", default="")

    args = parser.parse_args(argv)
    if args.command == "seed":
        snapshot = snapshot_from_seeds(mode=args.mode, progress=_print)
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2))
        print(f"Wrote {path} decisions={len(snapshot['decisions'])} prs={len(snapshot['prs'])}")
        return
    if args.command == "load":
        snapshot = load_json(args.path)
        graph = args.graph or graph_name_for_repo(snapshot["repo"])
        counts = load_snapshot(Store(), snapshot, graph, reset=args.reset)
        print(json.dumps({"graph": graph, **counts}))
        return
    snapshot = snapshot_from_pr(args.url, mode=args.mode, progress=_print)
    if args.out:
        Path(args.out).write_text(json.dumps(snapshot, indent=2))
    graph = graph_name_for_repo(snapshot["repo"])
    counts = load_snapshot(Store(), snapshot, graph, reset=True)
    print(json.dumps({"graph": graph, "decisions": len(snapshot["decisions"]), **counts}))


def _print(event: dict) -> None:
    print(json.dumps(event), file=sys.stderr)


if __name__ == "__main__":
    main()
