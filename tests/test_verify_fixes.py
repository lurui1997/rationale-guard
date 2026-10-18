"""Fixes from the independent DeepSeek verification pass."""

import importlib.util
import inspect
import threading
import time
from pathlib import Path

import httpx
import pytest

from rationale.db import Store, reset_db
from rationale.guard import _summary, run_guard
from rationale.ingest.github_api import GitHubClient, fetch_pr_diff
from rationale.ingest.load import load_snapshot
from rationale.judge import JudgeError, judge_candidates
from rationale.memory import record_correction
from rationale.premises import check_premises
from tests.test_graph import _mini


def _post_guard():
    path = Path(__file__).resolve().parents[1] / "action" / "post_guard.py"
    spec = importlib.util.spec_from_file_location("post_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_pr_diff_follows_redirect(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "encode" in request.url.path:
            return httpx.Response(
                302,
                headers={"location": "https://patch-diff.githubusercontent.com/raw/Kludex/starlette/pull/1.diff"},
            )
        return httpx.Response(200, text="diff --git a/x b/x\n+autoescape = True\n")

    real_client = httpx.Client

    def factory(*args, **kwargs):
        assert kwargs.get("follow_redirects") is True
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    text = fetch_pr_diff("encode/starlette", 1)
    assert "autoescape" in text


def test_github_client_follows_redirects(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/repos/encode/" in request.url.path:
            return httpx.Response(
                301,
                headers={"location": "https://api.github.com/repos/Kludex/starlette/pulls/1"},
            )
        return httpx.Response(200, json={"base": {"repo": {"full_name": "Kludex/starlette"}}})

    client = GitHubClient(cache_dir=tmp_path, token="", max_requests=4)
    assert client._client.follow_redirects is True
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    payload = client.pull("encode/starlette", 1)
    client.close()
    assert payload["base"]["repo"]["full_name"] == "Kludex/starlette"


def test_summary_names_unrelated_candidates():
    text = _summary("pass", [], [], judged=2, unrelated=2)
    assert "unrelated" in text
    assert "touches the changed files" not in text
    assert "touches the changed files" in _summary("pass", [], [], judged=0, unrelated=0)


def test_expert_lookups_do_not_write():
    assert "MERGE" not in inspect.getsource(__import__("rationale.experts", fromlist=["experts_for_paths"]).experts_for_paths)


def test_action_check_uses_pr_head_not_default_sha():
    post = _post_guard()
    comment_event = {"issue": {"number": 9, "pull_request": {"url": "https://api.github.com/repos/a/b/pulls/9"}}}
    assert post.event_head_sha(comment_event) is None
    pull_event = {"pull_request": {"number": 9, "head": {"sha": "abc123"}}}
    assert post.event_head_sha(pull_event) == "abc123"
    assert post.association_can_write("COLLABORATOR")
    assert post.association_can_write("OWNER")
    assert not post.association_can_write("CONTRIBUTOR")
    assert not post.association_can_write("NONE")


def test_llm_candidates_run_concurrently(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    state = {"current": 0, "max": 0}
    lock = threading.Lock()

    def slow(decision, parsed):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.25)
        with lock:
            state["current"] -= 1
        return {"verdict": "unrelated", "reason": "ok", "hunk_ref": parsed.paths[0] if parsed.paths else ""}

    monkeypatch.setattr("rationale.judge.judge_llm", slow)
    from rationale.diffutil import parse_diff

    parsed = parse_diff("diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-a\n+b\n")
    items = [
        {"id": "decision:a", "summary": "one", "source_quote": "one"},
        {"id": "decision:b", "summary": "two", "source_quote": "two"},
    ]
    verdicts = judge_candidates(items, parsed, "llm")
    assert len(verdicts) == 2
    assert state["max"] >= 2


def test_llm_timeout_is_not_an_offline_verdict(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setattr("rationale.judge.JUDGE_TIMEOUT", 0.05)

    def slow(decision, parsed):
        time.sleep(1.2)
        return {"verdict": "contradicts", "reason": "too late", "hunk_ref": ""}

    monkeypatch.setattr("rationale.judge.judge_llm", slow)
    from rationale.diffutil import parse_diff

    parsed = parse_diff("diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-a\n+b\n")
    items = [
        {"id": "decision:a", "summary": "one", "source_quote": "one"},
        {"id": "decision:b", "summary": "two", "source_quote": "two"},
    ]
    with pytest.raises(JudgeError, match="Offline judge was not used"):
        judge_candidates(items, parsed, "llm")


@pytest.fixture(scope="module")
def store():
    reset_db()
    current = Store()
    try:
        current.query("rationale_test_probe", "RETURN 1 AS ok", create=True)
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"FalkorDB is not reachable: {exc}")
    yield current
    try:
        current.delete_graph("rationale_test_probe")
    except Exception:
        pass


RISKY = """
diff --git a/src/billing.ts b/src/billing.ts
--- a/src/billing.ts
+++ b/src/billing.ts
@@ -1 +1 @@
-  const price = usage.cost ?? null;
+  const price = usage.cost ?? 0;
"""


def test_reingest_keeps_corrections_and_superseded_status(store):
    name = "rationale_test_merge"
    load_snapshot(store, _mini(), name, reset=True)
    record_correction(store, name, "decision:r1", "override", "billing now normalizes upstream", "casey")
    before = store.ping(name)
    load_snapshot(store, _mini(), name, reset=False)
    assert store.ping(name) >= before
    corrections, _trace = store.query(name, "MATCH (c:Correction) RETURN count(c) AS n")
    assert int(corrections[0]["n"]) >= 1
    status, _trace = store.query(
        name,
        "MATCH (d:Decision {id: 'decision:r1'}) RETURN d.status AS status",
    )
    assert status[0]["status"] == "superseded"
    store.delete_graph(name)


def test_override_stands_down_for_the_llm_judge(store, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    calls = []

    def always_contradict(decision, parsed):
        calls.append(decision.get("id"))
        return {"verdict": "contradicts", "reason": "mock says contradicts", "hunk_ref": "src/billing.ts"}

    monkeypatch.setattr("rationale.judge.judge_llm", always_contradict)
    name = "rationale_test_override_llm"
    load_snapshot(store, _mini(), name, reset=True)
    before = run_guard(store, "example/guard", RISKY, graph=name, judge="llm")
    assert before["status"] == "warn"
    assert "decision:r1" in calls
    record_correction(store, name, "decision:r1", "override", "billing now normalizes upstream", "casey")
    calls.clear()
    after = run_guard(store, "example/guard", RISKY, graph=name, judge="llm")
    assert after["status"] == "pass"
    assert not any(item["verdict"] == "contradicts" for item in after["findings"])
    assert not any(item.get("extractor") == "correction" for item in after["findings"])
    assert all(item["decision_id"] != "decision:r1" for item in after["findings"])
    assert any(row.get("kind") == "override" and row.get("decision_id") == "decision:r1" for row in after["learned"])
    assert calls == [] or "decision:r1" not in calls
    successors = [row.get("successor_id") for row in after["learned"] if row.get("successor_id")]
    assert successors
    assert not any(call in successors for call in calls)
    store.delete_graph(name)


def test_select_top_breaks_ties_for_the_max_age_shape():
    from rationale.guard import select_top

    candidates = []
    for index in range(10):
        candidates.append(
            {
                "id": f"decision:crowd{index}",
                "score": 3.65,
                "overlap": 0.0,
                "extracted_from": "pr:3436",
            }
        )
    candidates.append(
        {
            "id": "decision:maxage",
            "score": 3.65,
            "overlap": 0.62,
            "extracted_from": "pr:3651",
            "summary": "treat max_age=0 as explicit (is not None)",
        }
    )
    chosen = select_top(candidates)
    ids = [item["id"] for item in chosen]
    assert "decision:maxage" in ids
    assert ids.index("decision:maxage") < ids.index("decision:crowd0")
    capped = select_top(candidates, limit=3)
    assert sum(1 for item in capped if item["extracted_from"] == "pr:3436") <= 2
    assert capped[0]["id"] == "decision:maxage"


def test_scope_note_is_not_extracted():
    from rationale.ingest.extract import extract_offline, is_scope_note

    assert is_scope_note("extract only partitioned-cookie support from #2527, excluding persistence")
    assert is_scope_note("Persistence is not included in this PR.")
    text = (
        "This pull request is excluding persistence changes and they are not included in this PR.\n"
        "Preserve explicit zero prices and missing prices as distinct states."
    )
    decisions = extract_offline(
        {
            "source_id": "pr:3510",
            "text": text,
            "state": "MERGED",
            "kind": "pr",
            "url": "https://example.test/3510",
            "area": "sessions",
            "structured": True,
        }
    )
    blob = " ".join(item["source_quote"] for item in decisions).lower()
    assert "not included" not in blob
    assert "excluding persistence" not in blob
    assert any("distinct states" in item["source_quote"] for item in decisions)


def _sessions_snapshot() -> dict:
    now = int(time.time())
    file_change = {"path": "starlette/middleware/sessions.py", "change": "M", "additions": 4, "deletions": 2}

    def pr(number: int) -> dict:
        return {
            "number": number,
            "title": f"sessions {number}",
            "state": "MERGED",
            "merged_at": now,
            "created_at": now,
            "author": "alice",
            "url": f"https://example.test/pull/{number}",
            "body": "",
            "area": "sessions",
            "merge_sha": f"{number:040d}"[-40:],
            "files": [file_change],
            "fixes": [],
            "references": [],
            "supersedes": [],
        }

    decisions = []
    for index in range(10):
        quote = f"Session bookkeeping note {index} about cookie partitioning only."
        decisions.append(
            {
                "id": f"decision:crowd{index}",
                "summary": quote,
                "rationale": quote,
                "status": "accepted",
                "source_quote": quote,
                "source_url": "https://example.test/pull/3436",
                "confidence": 0.65,
                "extractor": "llm",
                "extracted_from": "pr:3436",
                "premises": [],
                "alternatives": [],
            }
        )
    decisions.append(
        {
            "id": "decision:maxage",
            "summary": "treat max_age=0 as explicit (is not None)",
            "rationale": "treat max_age=0 as explicit (is not None)",
            "status": "accepted",
            "source_quote": "treat max_age=0 as explicit (is not None)",
            "source_url": "https://example.test/pull/3651",
            "confidence": 0.65,
            "extractor": "llm",
            "extracted_from": "pr:3651",
            "premises": [],
            "alternatives": [],
        }
    )
    decisions.append(
        {
            "id": "decision:scope",
            "summary": "extract only partitioned-cookie support from #2527, excluding persistence",
            "rationale": "extract only partitioned-cookie support from #2527, excluding persistence",
            "status": "accepted",
            "source_quote": "extract only partitioned-cookie support from #2527, excluding persistence",
            "source_url": "https://example.test/pull/3510",
            "confidence": 0.9,
            "extractor": "llm",
            "extracted_from": "pr:3510",
            "premises": [],
            "alternatives": [],
        }
    )
    return {
        "repo": "example/sessions",
        "areas": [{"slug": "sessions", "name": "Sessions"}],
        "persons": [{"login": "alice", "is_bot": False, "last_active": now, "activity_evidence": "github"}],
        "issues": [],
        "docs": [],
        "prs": [pr(3436), pr(3651), pr(3510)],
        "reviews": [],
        "reverts": [],
        "decisions": decisions,
    }


MAX_AGE_DIFF = """
diff --git a/starlette/middleware/sessions.py b/starlette/middleware/sessions.py
--- a/starlette/middleware/sessions.py
+++ b/starlette/middleware/sessions.py
@@ -1 +1 @@
-        if self.max_age is not None:
+        if self.max_age:
"""


def test_sessions_max_age_is_judged_and_own_pr_is_excluded(store, monkeypatch):
    seen: list[str] = []

    def record(decisions, parsed, mode):
        verdicts = []
        for decision in decisions:
            seen.append(decision["id"])
            verdicts.append(
                {"verdict": "unrelated", "judge": mode, "reason": "recorded", "hunk_ref": parsed.paths[0] if parsed.paths else ""}
            )
        return verdicts

    monkeypatch.setattr("rationale.guard.judge_candidates", record)
    name = "rationale_test_sessions"
    load_snapshot(store, _sessions_snapshot(), name, reset=True)
    run_guard(store, "example/sessions", MAX_AGE_DIFF, graph=name, judge="offline")
    assert "decision:maxage" in seen
    assert "decision:scope" not in seen
    seen.clear()
    run_guard(store, "example/sessions", MAX_AGE_DIFF, graph=name, judge="offline", source_pr=3436)
    assert "decision:maxage" in seen
    assert not any(item.startswith("decision:crowd") for item in seen)
    store.delete_graph(name)


def test_inactive_owner_without_github_evidence_is_unknown(store):
    snapshot = _mini()
    for person in snapshot["persons"]:
        if person["login"] == "oldtimer":
            person["activity_evidence"] = "unknown"
    name = "rationale_test_activity"
    load_snapshot(store, snapshot, name, reset=True)
    summary, _traces = check_premises(store, name, ["decision:r4"])
    ids = [rule["id"] for rule in summary["decision:r4"]["rules"]]
    assert "R6_owner_inactive" not in ids
    assert "R6_activity_unknown" in ids
    store.delete_graph(name)


def test_visible_repos_skip_empty_graphs():
    from rationale.api.main import redact_client_text, visible_repos

    kept = visible_repos(
        [
            {"graph": "rationale__empty__repo", "decisions": 0},
            {"graph": "rationale__kept__repo", "decisions": 4},
        ]
    )
    assert [item["graph"] for item in kept] == ["rationale__kept__repo"]
    leaked = "Error 111 connecting to secret.db.internal:6379"
    assert redact_client_text(leaked) == "graph unavailable"
    assert "6379" not in redact_client_text("redis://secret.db.internal:6379/0")
    assert redact_client_text("Provide a unified diff or a public PR URL.") == "Provide a unified diff or a public PR URL."


def test_health_hides_db_host(monkeypatch):
    from fastapi.testclient import TestClient

    from rationale.api import main
    from rationale.db import GraphError

    main._seed_status["state"] = "ready"

    class Boom:
        def ping(self, _name):
            raise GraphError("Error 111 connecting to secret.db.internal:6379.")

        def query(self, *_args, **_kwargs):
            raise GraphError("Error 111 connecting to secret.db.internal:6379.")

        def list_graphs(self):
            raise GraphError("Error 111 connecting to secret.db.internal:6379.")

    monkeypatch.setattr(main, "_store", lambda: Boom())
    with TestClient(main.app) as client:
        response = client.get("/api/health")
    assert response.status_code == 503
    assert "6379" not in response.text
    assert "secret.db.internal" not in response.text
    assert "graph unavailable" in response.text


def test_second_guard_reuses_cached_graph_data(store, monkeypatch):
    from rationale.graphcache import invalidate

    name = "rationale_test_cache"
    load_snapshot(store, _mini(), name, reset=True)
    invalidate(name)
    real = store.query
    calls: list[str] = []

    def wrapped(graph, cypher, params=None, timeout=None):
        calls.append(cypher)
        return real(graph, cypher, params, timeout)

    monkeypatch.setattr(store, "query", wrapped)
    run_guard(store, "example/guard", RISKY, graph=name, judge="offline")
    first = len(calls)
    calls.clear()
    report = run_guard(store, "example/guard", RISKY, graph=name, judge="offline")
    blob = "\n".join(calls).lower()
    assert report["status"] == "warn"
    assert len(calls) < first
    assert "pagerank" not in blob
    assert "betweenness" not in blob
    assert "query_name" not in blob
    store.delete_graph(name)


def test_bad_graph_name_is_400_and_missing_graph_is_not_created():
    from fastapi.testclient import TestClient

    from rationale.api import main
    from rationale.db import GraphNotFound, Store

    name = "rationale_test_absent_graph"
    store = Store()
    if name in store.list_graphs():
        store.delete_graph(name)
    main._seed_status["state"] = "ready"
    diff = "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-a\n+b\n"
    with TestClient(main.app) as client:
        bad = client.get("/api/health", params={"graph": "not a graph"})
        missing = client.get("/api/health", params={"graph": name})
        guarded = client.post(
            "/api/guard",
            json={"repo": "example/missing", "graph": name, "diff": diff, "judge": "offline"},
        )
        malformed = client.post(
            "/api/guard",
            json={"repo": "example/missing", "graph": "bad name", "diff": diff, "judge": "offline"},
        )
    assert bad.status_code == 400
    assert "Invalid graph name" in bad.json()["detail"]
    assert "falkordb" not in bad.text.lower()
    assert missing.status_code == 404
    assert missing.json()["detail"] == "graph not found"
    assert guarded.status_code == 404
    assert guarded.json()["detail"] == "graph not found"
    assert "pass" not in guarded.text
    assert malformed.status_code == 400
    assert name not in Store().list_graphs()
    with pytest.raises(GraphNotFound):
        Store().ping(name)
