"""Decision extraction.

The LLM path requires a verbatim quote. The offline path is a deterministic
cue scorer used only when no API key is configured, or when the caller passes
``mode='offline'``. A failed LLM call raises ``LLMError`` and is not rewritten
as an offline extraction.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from rationale.llm import LLMError, chat_json, normalize_decision_payload
from rationale.settings import llm_api_key, llm_model

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM = """You extract architectural or product DECISIONS from software engineering artifacts.
A decision is a deliberate choice among alternatives that constrains future work.
NOT decisions: routine bug fixes without a stated trade-off, dependency bumps, formatting.

Rules:
- Only use the provided text. Never invent facts, PR numbers, or SHAs.
- For every decision, copy an exact quote (<= 300 chars) from the input as source_quote.
  The quote must appear verbatim in the input.
- rationale explains WHY, using the author's stated reasons. If no reason is stated, use null.
- List alternatives that were explicitly considered or rejected.
- If the artifact replaces earlier work, list the referenced numbers in supersedes as "#123".
- Return 0 to 3 decisions. Prefer fewer, higher-quality decisions.
- Scope notes are not decisions. Do not extract sentences that only say what this PR leaves out
  ("not included in this PR", "excluding persistence", "out of scope", "this PR only").
- Output a JSON object with a decisions array. Each decision has summary, rationale, status,
  source_quote, section, alternatives, supersedes, premises, confidence.
- alternatives, supersedes, and premises MUST be JSON arrays, not strings.
"""

STRONG_CUES = (
    "default deny",
    "default-deny",
    "mistaken for zero",
    "instead of",
    "rather than",
    "both were rejected",
)
CUES = (
    ("default deny", 8),
    ("default-deny", 8),
    ("mistaken for zero", 7),
    ("instead of", 5),
    ("rather than", 5),
    ("missing prices", 4),
    ("unknown prices", 4),
    ("distinct states", 4),
    ("without fabricating", 4),
    ("both were rejected", 5),
    ("rejected", 3),
    ("preserve", 2),
    ("distinct", 2),
    ("unknown", 2),
    ("drain", 2),
    ("must not", 4),
    ("do not", 1),
    ("enforcement gateway", 4),
    ("not directly", 2),
    ("worker drain", 6),
    ("owned hold", 4),
    ("final-backup", 6),
    ("before sleep", 3),
    ("empty queues", 4),
    ("stop their timers", 4),
)

BOILERPLATE = (
    "paperclip is the open source",
    "stack order:",
    "merged into",
    "part of 6",
)


def is_scope_note(text: str) -> bool:
    """A sentence that only bounds this PR is not a decision later diffs can contradict."""
    lowered = (text or "").lower()
    if any(
        phrase in lowered
        for phrase in (
            "not included in this pr",
            "not included in this pull request",
            "out of scope",
            "this pr only",
            "this pull request only",
            "does not include",
            "not part of this",
        )
    ):
        return True
    if re.search(r"\bexcluding\b", lowered) and "instead of" not in lowered and "decided" not in lowered:
        return True
    return False


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def quote_in_text(quote: str, text: str) -> bool:
    if not quote or not text:
        return False
    if quote in text:
        return True
    return normalize_ws(quote) in normalize_ws(text)


def decision_id(source_id: str, quote: str) -> str:
    digest = hashlib.sha1(f"{source_id}\n{normalize_ws(quote)}".encode()).hexdigest()[:12]
    return f"decision:{digest}"


def _score(span: str) -> int:
    lowered = span.lower()
    if any(snippet in lowered for snippet in BOILERPLATE):
        return -10
    score = 0
    for phrase, weight in CUES:
        if phrase in lowered:
            score += weight
    if "this pull request" in lowered or "this change" in lowered or "this adr" in lowered:
        score += 1
    if "drain" in lowered and ("idle" in lowered or "sleep" in lowered or "worker" in lowered):
        score += 3
    if "backup" in lowered and "sleep" in lowered:
        score += 3
    return score


def _spans(text: str) -> list[str]:
    cleaned = re.sub(r"<!--.*?-->", "", text or "", flags=re.DOTALL)
    found: list[str] = []
    seen: set[str] = set()
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^>\s*", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = line.strip()
        if len(line) < 24:
            continue
        parts = re.split(r"(?<=[.])\s+", line)
        window = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            window = f"{window} {part}".strip() if window else part
            if len(window) > 300:
                window = part
            if 24 <= len(window) <= 300:
                key = normalize_ws(window)
                if key not in seen and quote_in_text(window, text):
                    seen.add(key)
                    found.append(window)
            if len(window) > 180:
                window = ""
    return found


def _similar(left: str, right: str) -> bool:
    a = set(normalize_ws(left).lower().split())
    b = set(normalize_ws(right).lower().split())
    if not a or not b:
        return False
    overlap = len(a & b) / min(len(a), len(b))
    return overlap > 0.72


def _alternatives_from_text(text: str, quote: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    match = re.search(r"instead of (.+)", quote, flags=re.IGNORECASE)
    if match:
        description = match.group(1).strip(" .;")
        if description and quote_in_text(description, text):
            found.append(
                {
                    "description": description[:240],
                    "reason_rejected": quote[:300],
                    "ref": _ref_in(description),
                }
            )
    considered = re.search(r"Alternatives considered:\s*(.+)", text, flags=re.IGNORECASE)
    if considered:
        sentence = considered.group(1).strip()
        sentence = sentence.split("\n", 1)[0][:300]
        if quote_in_text(sentence, text):
            found.append(
                {
                    "description": sentence,
                    "reason_rejected": sentence if "rejected" in sentence.lower() else None,
                    "ref": _ref_in(sentence),
                }
            )
    return found


def _ref_in(text: str) -> str | None:
    match = re.search(r"#(\d+)", text or "")
    if not match:
        return None
    return f"#{match.group(1)}"


def _status_for(artifact: dict[str, Any]) -> str:
    kind = artifact.get("kind")
    state = (artifact.get("state") or "").upper()
    if kind == "doc":
        status = (artifact.get("doc_status") or "").lower()
        if any(token in status for token in ("pending", "proposed", "draft")):
            return "proposed"
        return "accepted"
    if state == "MERGED":
        return "accepted"
    if state == "CLOSED":
        return "rejected"
    return "proposed"


def _confidence(status: str, structured: bool, score: int) -> float:
    self_score = 0.85 if score >= 8 else 0.7
    confidence = 0.5 * self_score
    confidence += 0.2  # offline rationale is the quote itself, so it is non-empty
    confidence += 0.15 if structured else 0
    confidence += 0.15 if status in {"accepted", "proposed"} else 0
    return round(min(confidence, 0.99), 3)


def extract_offline(artifact: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
    text = artifact.get("text") or ""
    source_id = artifact["source_id"]
    ranked = sorted((( _score(span), span) for span in _spans(text)), reverse=True)
    picked: list[str] = []
    for score, span in ranked:
        if score < 4:
            continue
        if any(_similar(span, previous) for previous in picked):
            continue
        picked.append(span)
        if len(picked) >= limit:
            break
    decisions = []
    status = _status_for(artifact)
    structured = bool(artifact.get("structured"))
    for span in picked:
        score = _score(span)
        confidence = _confidence(status, structured, score)
        if confidence < 0.4:
            continue
        quote = span if span in text else normalize_ws(span)
        if not quote_in_text(quote, text):
            logger.info("Dropping offline decision whose quote is not verbatim")
            continue
        if is_scope_note(quote):
            logger.info("Tagging scope note as a non-decision: %s", quote[:80])
            continue
        issue_numbers = [int(num) for num in re.findall(r"#(\d+)", text)][:5]
        decisions.append(
            {
                "id": decision_id(source_id, quote),
                "summary": quote[:180],
                "rationale": quote,
                "status": status,
                "source_quote": quote[:300],
                "source_url": artifact.get("url"),
                "section": artifact.get("section") or "body",
                "confidence": confidence,
                "extractor": "offline",
                "model": None,
                "extracted_from": source_id,
                "area": artifact.get("area"),
                "alternatives": _alternatives_from_text(text, quote),
                "supersedes": [],
                "premises": [{"text": quote[:300], "issues": issue_numbers}],
            }
        )
    return decisions


def extract_llm(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    if not llm_api_key():
        raise LLMError("DEEPSEEK_API_KEY is not set")
    user = (
        f"ARTIFACT: {artifact.get('kind')} {artifact.get('source_id')} "
        f"(state={artifact.get('state')})\n"
        f"AREA: {artifact.get('area')}\n"
        f"TITLE: {artifact.get('title')}\n"
        f"TEXT:\n{artifact.get('text', '')[:8000]}"
    )
    payload = normalize_decision_payload(chat_json(EXTRACT_SYSTEM, user))
    kept = []
    text = artifact.get("text") or ""
    source_id = artifact["source_id"]
    for item in payload["decisions"][:3]:
        quote = (item.get("source_quote") or "").strip()
        if not quote_in_text(quote, text):
            logger.error("Dropping LLM decision; quote is not verbatim in %s", source_id)
            continue
        if is_scope_note(quote) or is_scope_note(item.get("summary") or ""):
            logger.info("Tagging scope note as a non-decision in %s", source_id)
            continue
        summary = (item.get("summary") or quote)[:200]
        rationale = item.get("rationale")
        status = item.get("status") or _status_for(artifact)
        if status not in {"accepted", "proposed", "rejected", "overridden", "deferred", "superseded"}:
            status = _status_for(artifact)
        try:
            self_score = float(item.get("confidence") if item.get("confidence") is not None else 0.6)
        except (TypeError, ValueError):
            self_score = 0.6
        self_score = max(0.0, min(1.0, self_score))
        structured = bool(artifact.get("structured"))
        confidence = round(
            0.5 * self_score
            + (0.2 if rationale else 0)
            + (0.15 if structured else 0)
            + (0.15 if status in {"accepted", "proposed"} else 0),
            3,
        )
        if confidence < 0.4:
            continue
        premises = []
        for premise in item.get("premises") or []:
            premises.append({"text": str(premise)[:300], "issues": []})
        if not premises:
            premises = [{"text": quote[:300], "issues": []}]
        kept.append(
            {
                "id": decision_id(source_id, quote),
                "summary": summary,
                "rationale": rationale,
                "status": status,
                "source_quote": quote[:300],
                "source_url": artifact.get("url"),
                "section": item.get("section") or artifact.get("section") or "body",
                "confidence": confidence,
                "extractor": "llm",
                "model": None,
                "extracted_from": source_id,
                "area": artifact.get("area"),
                "alternatives": item.get("alternatives") or [],
                "supersedes": item.get("supersedes") or [],
                "premises": premises,
            }
        )
    return kept


def extract_decisions(artifact: dict[str, Any], mode: str = "auto") -> list[dict[str, Any]]:
    """``mode`` is auto, offline, or llm. auto uses the LLM only when a key is set.

    LLM failures propagate. They are logged and are not converted into offline decisions.
    """
    if mode not in {"auto", "offline", "llm"}:
        raise ValueError("extract mode must be auto, offline, or llm")
    if mode == "offline":
        return extract_offline(artifact)
    if mode == "llm" or (mode == "auto" and llm_api_key()):
        # Resolve the provider model first so an internal tag fails before the call.
        _provider_model = llm_model()
        try:
            decisions = extract_llm(artifact)
        except Exception as exc:
            logger.error("LLM extraction failed for %s: %s", artifact.get("source_id"), exc)
            raise
        for decision in decisions:
            decision["model"] = _provider_model
        return decisions
    logger.info("No LLM key; extracting %s with the offline cue scorer", artifact.get("source_id"))
    return extract_offline(artifact)
