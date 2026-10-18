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
        current.query("rationale_test_probe", "RETURN 1 AS ok")
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"FalkorDB is not reachable: {exc}")
    return current


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
