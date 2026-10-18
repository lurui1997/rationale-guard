"""Verdicts for one graph candidate.

``llm`` never falls back to ``offline``. A provider error is logged and raised
as ``JudgeError``. The offline judge is used only when the caller asked for it
or when ``auto`` resolves to offline because no API key is configured.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any

from rationale.diffutil import ParsedDiff
from rationale.llm import LLMError, chat_json, normalize_verdict_payload
from rationale.settings import guard_judge_mode, llm_api_key, llm_model

logger = logging.getLogger(__name__)

# One provider call should finish inside this window. Candidates run together so a
# diff does not pay the sum of five sequential round trips. p50 target is under 3s.
JUDGE_TIMEOUT = 8.0


class JudgeError(RuntimeError):
    """The requested judge could not produce a verdict. Do not substitute the other judge."""


JUDGE_SYSTEM = """You decide whether a code diff contradicts one engineering decision.
The decision was retrieved from a graph. Do not invent decisions, numbers, or paths.
Return a JSON object with keys verdict, reason, hunk_ref.
verdict is exactly one of: contradicts, related, unrelated.
reason is at most 40 words and includes a short verbatim fragment of QUOTE.
hunk_ref is the file path the verdict is about, copied from the diff.
"""

ZERO_PATTERNS = (
    r"\?\?\s*0\b",
    r"\|\|\s*0\b",
    r"\?\?\s*0",
    r"cost\w*\s*[:=]\s*0\b",
    r"unknown\s*(?:as|to)\s*(?:zero|0)",
    r"(?:null|undefined)\s*(?:\?\?|\|\|)\s*0\b",
    r"costcents\s*[:=]\s*0\b",
    r"price\s*[:=]\s*0\b",
)


def resolve_judge_mode(requested: str | None = None) -> str:
    mode = (requested or guard_judge_mode() or "auto").lower()
    if mode not in {"auto", "offline", "llm"}:
        raise JudgeError("judge mode must be auto, offline, or llm")
    if mode == "llm":
        if not llm_api_key():
            raise JudgeError("LLM judge was requested but DEEPSEEK_API_KEY is not set")
        llm_model()
        return "llm"
    if mode == "offline":
        return "offline"
    if llm_api_key():
        llm_model()
        return "llm"
    logger.info("GUARD_JUDGE=auto and no API key; using the offline judge")
    return "offline"


def judge_candidates(decisions: list[dict[str, Any]], parsed: ParsedDiff, mode: str) -> list[dict[str, Any]]:
    """Judge every candidate. LLM calls run concurrently and time out instead of falling back."""
    if len(decisions) <= 1 or mode != "llm":
        return [judge_candidate(item, parsed, mode) for item in decisions]
    workers = min(8, len(decisions))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(judge_candidate, item, parsed, mode) for item in decisions]
        try:
            return [future.result(timeout=JUDGE_TIMEOUT + 1) for future in futures]
        except FuturesTimeout as exc:
            pool.shutdown(wait=False, cancel_futures=True)
            raise JudgeError("LLM judge timed out. Offline judge was not used.") from exc
        except Exception:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def judge_candidate(decision: dict[str, Any], parsed: ParsedDiff, mode: str) -> dict[str, Any]:
    if decision.get("extractor") == "correction":
        return {
            "verdict": "unrelated",
            "judge": mode,
            "reason": "This node is an override note. It permits the change and is not a decision to contradict.",
            "hunk_ref": parsed.paths[0] if parsed.paths else "",
        }
    if mode == "offline":
        verdict = judge_offline(decision, parsed)
        verdict["judge"] = "offline"
        return verdict
    if mode != "llm":
        raise JudgeError(f"unknown judge mode {mode}")
    try:
        verdict = judge_llm(decision, parsed)
    except Exception as exc:
        logger.error(
            "LLM judge failed for %s: %s",
            decision.get("id"),
            exc,
        )
        raise JudgeError(
            f"LLM judge failed for {decision.get('id')}: {exc}. Offline judge was not used."
        ) from exc
    verdict["judge"] = "llm"
    return verdict


def judge_llm(decision: dict[str, Any], parsed: ParsedDiff) -> dict[str, Any]:
    hunks = []
    for item in parsed.files[:6]:
        added = "\n".join(f"+{line}" for line in item.added[:40])
        removed = "\n".join(f"-{line}" for line in item.removed[:40])
        hunks.append(f"FILE {item.path}\n{removed}\n{added}")
    user = (
        f"DECISION_ID: {decision.get('id')}\n"
        f"SUMMARY: {decision.get('summary')}\n"
        f"QUOTE: {decision.get('source_quote')}\n"
        f"STATUS: {decision.get('status')}\n"
        f"DIFF:\n" + "\n".join(hunks)
    )
    try:
        payload = chat_json(JUDGE_SYSTEM, user, timeout=JUDGE_TIMEOUT)
        parsed_payload = normalize_verdict_payload(payload)
    except (LLMError, ValueError) as exc:
        raise JudgeError(str(exc)) from exc
    return {
        "verdict": parsed_payload["verdict"],
        "reason": parsed_payload["reason"][:280],
        "hunk_ref": parsed_payload["hunk_ref"] or (parsed.paths[0] if parsed.paths else ""),
    }


def judge_offline(decision: dict[str, Any], parsed: ParsedDiff) -> dict[str, Any]:
    quote = f"{decision.get('source_quote') or ''} {decision.get('summary') or ''}"
    quote_l = quote.lower()
    added = "\n".join(item.added_text() for item in parsed.files)
    removed = "\n".join(item.removed_text() for item in parsed.files)
    added_l = added.lower()
    removed_l = removed.lower()
    paths = " ".join(parsed.paths).lower()
    hunk = parsed.paths[0] if parsed.paths else ""

    if parsed.only_docs_or_comments() and not _deny_flip(quote_l, added_l, removed_l):
        return _pack("unrelated", "Diff changes comments or documentation only.", hunk)

    reasons: list[str] = []
    if _unknown_to_zero(quote_l, added_l, removed_l, paths):
        reasons.append(
            "Added lines collapse unknown or null cost into 0. The decision keeps those states distinct."
        )
    if _deny_flip(quote_l, added_l, removed_l):
        reasons.append("Added lines replace default-deny with an allow.")
    if _idle_reversal(quote_l, added_l):
        reasons.append("Added lines drop the drain, fence, or backup checkpoint the decision requires.")
    if _alternative_overlap(decision, added_l):
        reasons.append("Added lines restate an alternative this decision rejected.")
    if decision.get("status") == "rejected" and _content_overlap(quote, added) >= 0.45:
        reasons.append("Added lines follow a change that was closed without merging.")

    if reasons:
        return _pack("contradicts", reasons[0], hunk)
    if _content_overlap(quote, added + "\n" + removed) >= 0.22:
        return _pack("related", "The diff shares the decision's topic and does not reverse the quote.", hunk)
    return _pack("unrelated", "No reversal between the diff and the decision quote.", hunk)


def _pack(verdict: str, reason: str, hunk: str) -> dict[str, str]:
    return {"verdict": verdict, "reason": reason, "hunk_ref": hunk}


def _unknown_to_zero(quote: str, added: str, removed: str, paths: str) -> bool:
    if not any(token in quote for token in ("unknown", "unpriced", "missing price", "mistaken for zero", "null")):
        return False
    if not any(token in quote for token in ("zero", "0", "distinct", "preserve", "unpriced")):
        return False
    costish = any(token in paths or token in added for token in ("cost", "price", "billing", "usage"))
    if not costish:
        return False
    if any(re.search(pattern, added) for pattern in ZERO_PATTERNS):
        return True
    if re.search(r"\b0\b", added) and any(token in removed for token in ("null", "unknown", "unpriced")):
        return True
    return False


def _deny_flip(quote: str, added: str, removed: str) -> bool:
    if "default deny" not in quote and "default-deny" not in quote:
        return False
    if re.search(r"default[-\s]?allow|defaultdeny\s*=\s*false|policy\s*[:=]\s*['\"]allow['\"]", added):
        return True
    if ("default deny" in removed or "default-deny" in removed) and "allow" in added:
        return True
    return False


def _idle_reversal(quote: str, added: str) -> bool:
    if not any(token in quote for token in ("drain", "fence", "onidledrain", "backup checkpoint", "final-backup", "final backup")):
        return False
    if any(token in added for token in ("stop polling", "stop the timer", "disable drain", "skip drain", "skip the backup", "disable backup")):
        return True
    return False


def _alternative_overlap(decision: dict[str, Any], added: str) -> bool:
    for alt in decision.get("alternatives") or []:
        description = alt.get("description") or ""
        if description and _content_overlap(description, added) >= 0.55:
            return True
    return False


def _tokens(text: str) -> set[str]:
    stop = {"this", "that", "with", "from", "have", "were", "been", "into", "only", "when", "they", "them", "than", "does", "will"}
    return {token for token in re.findall(r"[a-z][a-z0-9]{4,}", text.lower()) if token not in stop}


def _content_overlap(left: str, right: str) -> float:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
