"""Report recall, false positives, and latency for labelled guard diffs.

``python -m eval`` uses the offline judge.
``python -m eval --judge llm`` fails (non-zero) if any case does not receive an
LLM verdict, including when the judge would otherwise have fallen back.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from rationale.db import Store, graph_name_for_repo
from rationale.guard import run_guard
from rationale.ingest.load import load_json, load_snapshot
from rationale.judge import JudgeError
from rationale.settings import repo_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval")
    parser.add_argument("--judge", choices=["offline", "llm"], default="offline")
    parser.add_argument("--cases", default=str(repo_root() / "eval" / "cases.yaml"))
    parser.add_argument("--seed", default=str(repo_root() / "data" / "seed" / "paperclip.json"))
    parser.add_argument("--graph", default=None)
    parser.add_argument("--skip-load", action="store_true")
    args = parser.parse_args(argv)
    cases = yaml.safe_load(Path(args.cases).read_text())
    snapshot = load_json(args.seed)
    graph = args.graph or graph_name_for_repo(snapshot["repo"])
    store = Store()
    if not args.skip_load:
        # The benchmark is the committed seed, not corrections left in a local graph.
        load_snapshot(store, snapshot, graph, reset=True)
    rows = []
    fallbacks = []
    errors = []
    for case in cases:
        started = time.perf_counter()
        try:
            report = run_guard(store, case.get("repo") or snapshot["repo"], case["diff"], graph=graph, judge=args.judge)
        except JudgeError as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            errors.append(case["id"])
            rows.append({"id": case["id"], "label": case["label"], "error": str(exc), "ms": elapsed, "hit": False, "judge": "error"})
            print(f"ERROR {case['id']}: {exc}", file=sys.stderr)
            continue
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        hit = report["status"] == "warn" and any(item["verdict"] == "contradicts" for item in report["findings"])
        judges = {item["judge"] for item in report["findings"]} or {report["judge"]}
        if args.judge == "llm" and judges != {"llm"}:
            fallbacks.append(case["id"])
        rows.append(
            {
                "id": case["id"],
                "label": case["label"],
                "suite": case.get("suite") or "cue",
                "hit": hit,
                "status": report["status"],
                "findings": len(report["findings"]),
                "graph_ms": report["graph_ms"],
                "ms": elapsed,
                "judge": ",".join(sorted(judges)),
            }
        )
    positives = [row for row in rows if row["label"] == "violates"]
    negatives = [row for row in rows if row["label"] == "benign"]
    true_positives = [row for row in positives if row["hit"]]
    false_positives = [row for row in negatives if row["hit"]]
    recall = (len(true_positives) / len(positives)) if positives else 0.0
    false_positive_rate = (len(false_positives) / len(negatives)) if negatives else 0.0
    graph_times = sorted(row["graph_ms"] for row in rows if "graph_ms" in row)
    total_times = sorted(row["ms"] for row in rows)
    print("| id | label | caught | status | findings | judge | graph_ms | total_ms |")
    print("|---|---|---|---|---|---|---|---|")
    for row in rows:
        if row.get("error"):
            print(f"| {row['id']} | {row['label']} | error |  |  | error |  | {row['ms']} |")
            continue
        print(
            f"| {row['id']} | {row['label']} | {str(row['hit']).lower()} | {row['status']} | "
            f"{row['findings']} | {row['judge']} | {row['graph_ms']} | {row['ms']} |"
        )
    print()
    print(f"cases: {len(rows)}")
    print(f"recall: {recall:.2f} ({len(true_positives)}/{len(positives)})")
    print(f"false_positives: {len(false_positives)}/{len(negatives)} ({false_positive_rate:.2f})")
    for suite in ("cue", "paraphrase"):
        suite_rows = [row for row in rows if row.get("suite") == suite and not row.get("error")]
        if not suite_rows:
            continue
        suite_pos = [row for row in suite_rows if row["label"] == "violates"]
        suite_neg = [row for row in suite_rows if row["label"] == "benign"]
        suite_tp = [row for row in suite_pos if row["hit"]]
        suite_fp = [row for row in suite_neg if row["hit"]]
        suite_recall = (len(suite_tp) / len(suite_pos)) if suite_pos else 0.0
        print(
            f"suite {suite}: recall {suite_recall:.2f} ({len(suite_tp)}/{len(suite_pos)}) "
            f"false_positives {len(suite_fp)}/{len(suite_neg)}"
        )
    if graph_times:
        print(f"graph_ms_p50: {_percentile(graph_times, 0.5)}")
        print(f"total_ms_p50: {_percentile(total_times, 0.5)}")
    if args.judge == "llm" and (fallbacks or errors):
        print(f"LLM judge did not produce every verdict. fallbacks={fallbacks} errors={errors}", file=sys.stderr)
        return 1
    return 0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
    return values[index]


if __name__ == "__main__":
    raise SystemExit(main())
