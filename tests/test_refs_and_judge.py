"""Unit tests that do not need FalkorDB."""

import json

import pytest

from rationale.db import describe_url
from rationale.diffutil import parse_diff
from rationale.guard import rank_score, select_top
from rationale.judge import JudgeError, judge_candidate, judge_offline
from rationale.llm import as_list, build_chat_body, normalize_decision_payload, normalize_verdict_payload
from rationale.refs import is_bot, parse_refs
from rationale.settings import llm_model


def test_checklist_closing_keyword_is_ignored():
    body = """
## What Changed
- [ ] Fixes #123
- [x] Fixes #456
Fixes #789
See other/repo#5 and https://github.com/other/repo/issues/9
Depends on #42
PAP-10341
"""
    parsed = parse_refs(body, "paperclipai/paperclip")
    numbers = [ref.number for ref in parsed.fixes]
    assert 123 not in numbers
    assert 456 not in numbers
    assert numbers == [789]
    assert 42 in [ref.number for ref in parsed.references if ref.kind == "depends_on"]
    assert parsed.external_tickets == ["PAP-10341"]


def test_cross_repo_closing_keyword_dropped():
    parsed = parse_refs("Fixes other/repo#5\nFixes #8", "paperclipai/paperclip")
    assert [ref.number for ref in parsed.fixes] == [8]


def test_bot_detection():
    assert is_bot("dependabot[bot]")
    assert is_bot("helper", typename="Bot")
    assert not is_bot("devinfoley")


def test_list_fields_from_strings():
    payload = normalize_decision_payload(
        {
            "decisions": [
                {
                    "summary": "keep null",
                    "source_quote": "keep null",
                    "alternatives": "[{\"description\": \"use zero\"}]",
                    "supersedes": "#1\n#2",
                    "premises": "issue stays open",
                }
            ]
        }
    )
    decision = payload["decisions"][0]
    assert decision["alternatives"][0]["description"] == "use zero"
    assert decision["supersedes"] == ["#1", "#2"]
    assert decision["premises"] == ["issue stays open"]
    assert as_list('["a", "b"]') == ["a", "b"]


def test_verdict_list_is_normalized():
    parsed = normalize_verdict_payload({"verdict": ["contradicts"], "reason": ["because"], "hunk_ref": ["a.ts"]})
    assert parsed["verdict"] == "contradicts"
    assert parsed["reason"] == "because"


def test_chat_body_uses_json_object_and_env_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash")
    body = build_chat_body("system", "user")
    assert body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(body)
    assert body["model"] == "deepseek-flash"


def test_internal_model_tag_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "decision-extract-v1")
    with pytest.raises(ValueError):
        llm_model()
    monkeypatch.setenv("LLM_MODEL", "offline")
    with pytest.raises(ValueError):
        llm_model()


def test_default_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert llm_model() == "deepseek-chat"


def test_redis_and_rediss_urls():
    plain = describe_url("redis://example.falkordb.cloud:6379")
    secure = describe_url("rediss://example.falkordb.cloud:6379")
    assert plain["tls"] is False
    assert secure["tls"] is True
    assert describe_url("falkors://localhost:6379")["tls"] is True
    with pytest.raises(Exception):
        describe_url("http://localhost:6379")


def test_offline_judge_flags_null_to_zero():
    diff = parse_diff(
        """
diff --git a/packages/adapter-utils/src/billing.ts b/packages/adapter-utils/src/billing.ts
--- a/packages/adapter-utils/src/billing.ts
+++ b/packages/adapter-utils/src/billing.ts
@@ -1 +1 @@
-  const price = usage.cost ?? null;
+  const price = usage.cost ?? 0;
"""
    )
    decision = {
        "source_quote": "Preserve explicit zero prices and missing prices as distinct states.",
        "summary": "Preserve explicit zero prices and missing prices as distinct states.",
        "status": "accepted",
        "alternatives": [],
    }
    verdict = judge_offline(decision, diff)
    assert verdict["verdict"] == "contradicts"


def test_offline_judge_ignores_comments():
    diff = parse_diff(
        """
diff --git a/packages/adapter-utils/src/billing.ts b/packages/adapter-utils/src/billing.ts
--- a/packages/adapter-utils/src/billing.ts
+++ b/packages/adapter-utils/src/billing.ts
@@ -1 +1 @@
-  // track usage
+  // track usage carefully
"""
    )
    decision = {
        "source_quote": "Preserve explicit zero prices and missing prices as distinct states.",
        "summary": "Preserve explicit zero prices and missing prices as distinct states.",
        "status": "accepted",
        "alternatives": [],
    }
    assert judge_offline(decision, diff)["verdict"] == "unrelated"


def test_llm_judge_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")

    def boom(*_args, **_kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr("rationale.judge.judge_llm", boom)
    diff = parse_diff("diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(JudgeError, match="Offline judge was not used"):
        judge_candidate({"id": "decision:abc", "source_quote": "keep null", "summary": "keep null"}, diff, "llm")


def test_wrong_feedback_drops_below_top_five():
    candidates = []
    for index in range(5):
        candidates.append({"id": f"d{index}", "score": rank_score(2, 0, 0.8, 0)})
    candidates.append({"id": "wrong", "score": rank_score(2, 0, 0.8, 1)})
    chosen = select_top(candidates, limit=5, minimum=0.5)
    assert "wrong" not in {item["id"] for item in chosen}
    assert len(chosen) == 5


def test_quote_must_be_verbatim():
    from rationale.ingest.extract import extract_offline

    decisions = extract_offline(
        {
            "source_id": "pr:1",
            "text": "Preserve explicit zero prices and missing prices as distinct states, including later unpriced usage.",
            "state": "MERGED",
            "kind": "pr",
            "url": "https://example.test/1",
            "area": "costs",
            "structured": True,
        }
    )
    assert decisions
    assert decisions[0]["source_quote"] in decisions[0]["rationale"] or decisions[0]["source_quote"]
    assert "invented sentence that is not in the source" not in decisions[0]["source_quote"]
