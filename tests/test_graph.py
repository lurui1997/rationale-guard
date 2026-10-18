"""Integration tests against a live FalkorDB."""

import time

import pytest

from rationale.db import GraphError, Store, reset_db
from rationale.guard import run_guard
from rationale.ingest.load import load_snapshot
from rationale.memory import record_correction
from rationale.premises import check_premises


@pytest.fixture(scope="module")
def store():
    reset_db()
    try:
        current = Store()
        current.query("rationale_test_probe", "RETURN 1 AS ok")
    except Exception as exc:  # pragma: no cover - environment failure
        pytest.fail(f"FalkorDB is not reachable: {exc}")
    return current


def _mini() -> dict:
    now = int(time.time())
    return {
        "repo": "example/guard",
        "areas": [
            {"slug": "costs", "name": "Costs"},
            {"slug": "idle-sleep", "name": "Idle"},
        ],
        "persons": [
            {"login": "alice", "is_bot": False, "last_active": now},
            {"login": "oldtimer", "is_bot": False, "last_active": now - 200 * 86400},
            {"login": "dependabot[bot]", "is_bot": True, "last_active": now},
        ],
        "issues": [
            {"number": 1, "title": "unknown cost", "state": "OPEN", "created_at": now, "author": "alice", "url": "https://example.test/issues/1", "body": "open", "area": "costs"},
            {"number": 2, "title": "closed", "state": "CLOSED", "created_at": now, "author": "alice", "url": "https://example.test/issues/2", "body": "closed", "area": "costs"},
        ],
        "docs": [
            {"path": "doc/deleted.md", "kind": "plan", "title": "deleted", "status": "", "deleted_in": "abc123", "area": "costs", "url": "https://example.test/deleted"},
            {"path": "doc/adr.md", "kind": "adr", "title": "adr", "status": "pending SecurityEngineer validation", "area": "costs", "url": "https://example.test/adr"},
        ],
        "prs": [
            {
                "number": 10,
                "title": "preserve unknown",
                "state": "MERGED",
                "merged_at": now,
                "created_at": now,
                "author": "alice",
                "url": "https://example.test/pull/10",
                "body": "Fixes #1",
                "area": "costs",
                "merge_sha": "a" * 40,
                "files": [{"path": "src/billing.ts", "change": "M", "additions": 3, "deletions": 1}],
                "fixes": [{"number": 1, "kind": "fixes", "evidence": "Fixes #1", "via": "closing_keyword"}],
                "references": [],
                "supersedes": [],
            },
            {
                "number": 11,
                "title": "closed issue fix",
                "state": "MERGED",
                "merged_at": now,
                "created_at": now,
                "author": "alice",
                "url": "https://example.test/pull/11",
                "body": "Fixes #2",
                "area": "costs",
                "merge_sha": "b" * 40,
                "files": [{"path": "src/other.ts", "change": "M", "additions": 1, "deletions": 0}],
                "fixes": [{"number": 2, "kind": "fixes", "evidence": "Fixes #2", "via": "closing_keyword"}],
                "references": [],
                "supersedes": [],
            },
            {
                "number": 20,
                "title": "newer approach",
                "state": "OPEN",
                "merged_at": None,
                "created_at": now,
                "author": "alice",
                "url": "https://example.test/pull/20",
                "body": "supersedes #21",
                "area": "costs",
                "merge_sha": "",
                "files": [{"path": "src/new.ts", "change": "M", "additions": 1, "deletions": 0}],
                "fixes": [],
                "references": [],
                "supersedes": [{"number": 21, "kind": "supersedes", "evidence": "supersedes #21", "via": "supersede"}],
            },
            {
                "number": 21,
                "title": "old approach",
                "state": "MERGED",
                "merged_at": now - 10,
                "created_at": now - 10,
                "author": "oldtimer",
                "url": "https://example.test/pull/21",
                "body": "Depends on #20",
                "area": "costs",
                "merge_sha": "c" * 40,
                "files": [{"path": "src/old.ts", "change": "M", "additions": 1, "deletions": 0}],
                "fixes": [],
                "references": [{"number": 20, "kind": "depends_on", "evidence": "Depends on #20", "via": "depends"}],
                "supersedes": [],
            },
        ],
        "reviews": [],
        "reverts": [{"commit": "d" * 40, "reverts": "c" * 40, "subject": "Revert old approach"}],
        "decisions": [
            {
                "id": "decision:r1",
                "summary": "Preserve unknown prices instead of zero.",
                "rationale": "Preserve unknown prices instead of zero.",
                "status": "accepted",
                "source_quote": "Preserve unknown prices instead of zero.",
                "source_url": "https://example.test/pull/10",
                "confidence": 0.9,
                "extractor": "offline",
                "extracted_from": "pr:10",
                "area": "costs",
                "premises": [{"text": "Preserve unknown prices instead of zero.", "issues": [1]}],
                "alternatives": [{"description": "treating unknown prices as zero", "reason_rejected": "they are not proven", "ref": ""}],
            },
            {
                "id": "decision:r1neg",
                "summary": "Closed issue stays closed.",
                "rationale": "Closed issue stays closed.",
                "status": "accepted",
                "source_quote": "Closed issue stays closed.",
                "source_url": "https://example.test/pull/11",
                "confidence": 0.8,
                "extractor": "offline",
                "extracted_from": "pr:11",
                "area": "costs",
                "premises": [],
                "alternatives": [],
            },
            {
                "id": "decision:r2",
                "summary": "Deleted plan.",
                "rationale": "Deleted plan.",
                "status": "accepted",
                "source_quote": "Deleted plan.",
                "source_url": "https://example.test/deleted",
                "confidence": 0.8,
                "extractor": "offline",
                "extracted_from": "doc:doc/deleted.md",
                "area": "costs",
                "premises": [],
                "alternatives": [],
            },
            {
                "id": "decision:r3",
                "summary": "External tools are default deny.",
                "rationale": "External tools are default deny.",
                "status": "proposed",
                "source_quote": "External tools are default deny.",
                "source_url": "https://example.test/adr",
                "confidence": 0.8,
                "extractor": "offline",
                "extracted_from": "doc:doc/adr.md",
                "area": "costs",
                "premises": [],
                "alternatives": [],
            },
            {
                "id": "decision:r4",
                "summary": "Old approach.",
                "rationale": "Old approach.",
                "status": "accepted",
                "source_quote": "Old approach.",
                "source_url": "https://example.test/pull/21",
                "confidence": 0.8,
                "extractor": "offline",
                "extracted_from": "pr:21",
                "area": "costs",
                "premises": [],
                "alternatives": [],
            },
        ],
    }


@pytest.fixture(scope="module")
def graph(store):
    name = "rationale_test_mini"
    load_snapshot(store, _mini(), name, reset=True)
    # Second load is idempotent: node count stays put.
    before = store.ping(name)
    load_snapshot(store, _mini(), name, reset=False)
    assert store.ping(name) == before
    return name


def test_premise_rules_hit_their_positive_examples(store, graph):
    summary, traces = check_premises(store, graph, ["decision:r1", "decision:r1neg", "decision:r2", "decision:r3", "decision:r4"])
    assert traces
    assert any(rule["id"] == "R1_fixed_issue_still_open" for rule in summary["decision:r1"]["rules"])
    assert not any(rule["id"] == "R1_fixed_issue_still_open" for rule in summary["decision:r1neg"]["rules"])
    assert any(rule["id"] == "R2_source_deleted" for rule in summary["decision:r2"]["rules"])
    assert summary["decision:r2"]["status"] == "stale"
    assert any(rule["id"] == "R3_adr_not_accepted" for rule in summary["decision:r3"]["rules"])
    assert any(rule["id"] == "R4_superseded" for rule in summary["decision:r4"]["rules"])
    assert any(rule["id"] == "R5_dependency_unmerged" for rule in summary["decision:r4"]["rules"])
    assert any(rule["id"] == "R6_owner_inactive" for rule in summary["decision:r4"]["rules"])
    assert any(rule["id"] == "R7_reverted_code" for rule in summary["decision:r4"]["rules"])


def test_guard_flags_cost_reversal_and_passes_a_comment(store, graph):
    risky = """
diff --git a/src/billing.ts b/src/billing.ts
--- a/src/billing.ts
+++ b/src/billing.ts
@@ -1 +1 @@
-  const price = usage.cost ?? null;
+  const price = usage.cost ?? 0;
"""
    benign = """
diff --git a/src/billing.ts b/src/billing.ts
--- a/src/billing.ts
+++ b/src/billing.ts
@@ -1 +1 @@
-  // note
+  // note for reviewers
"""
    warned = run_guard(store, "example/guard", risky, graph=graph, judge="offline")
    assert warned["status"] == "warn"
    assert warned["graph_ms"] < 1000
    assert any(item["decision_id"] == "decision:r1" and item["verdict"] == "contradicts" for item in warned["findings"])
    finding = next(item for item in warned["findings"] if item["decision_id"] == "decision:r1")
    assert finding["quote"]
    assert finding["cypher_trace"] or warned["cypher_trace"]
    assert finding["subgraph"]["nodes"]
    assert finding["rejected_alternatives"]
    assert any(person["login"] == "alice" for person in finding["ask"])
    passed = run_guard(store, "example/guard", benign, graph=graph, judge="offline")
    assert passed["status"] == "pass"
    assert not any(item["verdict"] == "contradicts" for item in passed["findings"])


def test_override_changes_the_next_guard(store, graph):
    record_correction(store, graph, "decision:r1", "override", "billing now normalizes upstream", "casey", pr=99)
    risky = """
diff --git a/src/billing.ts b/src/billing.ts
--- a/src/billing.ts
+++ b/src/billing.ts
@@ -1 +1 @@
-  const price = usage.cost ?? null;
+  const price = usage.cost ?? 0;
"""
    report = run_guard(store, "example/guard", risky, graph=graph, judge="offline")
    assert not any(item["decision_id"] == "decision:r1" and item["verdict"] == "contradicts" for item in report["findings"])
    assert any(row.get("decision_id") == "decision:r1" and row.get("kind") == "override" for row in report["learned"])
    rows, _trace = store.query(
        graph,
        "MATCH (n:Decision)-[:SUPERSEDES]->(d:Decision {id: 'decision:r1'}) RETURN n.summary AS summary, d.status AS status",
    )
    assert rows
    assert rows[0]["status"] == "superseded"
    assert "normalizes upstream" in rows[0]["summary"]


def test_graph_error_is_not_an_empty_result():
    from rationale.db import connect

    with pytest.raises(GraphError, match="connection failed|query failed"):
        connect("redis://127.0.0.1:1")
