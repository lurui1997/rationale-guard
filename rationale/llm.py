"""OpenAI-compatible JSON client.

DeepSeek accepts ``response_format: {type: json_object}`` and does not accept
``json_schema``. List-shaped fields sometimes come back as strings; callers
normalize those. The model id is always ``LLM_MODEL`` (default ``deepseek-chat``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from rationale.settings import llm_api_key, llm_base_url, llm_model

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The provider failed. Callers must not replace this with an offline result."""


def as_list(value: Any) -> list[Any]:
    """Normalize a field that should be a list. Strings are parsed, not trusted as one item of prose unless needed."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        parts = [part.strip() for part in re.split(r"[\n;]", text) if part.strip()]
        return parts or [text]
    if isinstance(value, dict):
        return [value]
    return [value]


def normalize_decision_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise LLMError("LLM JSON was not an object")
    decisions = as_list(payload.get("decisions"))
    cleaned = []
    for item in decisions:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError as exc:
                raise LLMError(f"decision entry was a string and not JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise LLMError("decision entry was not an object")
        item = dict(item)
        item["alternatives"] = [_alt(entry) for entry in as_list(item.get("alternatives"))]
        item["supersedes"] = [str(entry) for entry in as_list(item.get("supersedes"))]
        item["premises"] = [str(entry) for entry in as_list(item.get("premises"))]
        cleaned.append(item)
    return {"decisions": cleaned}


def _alt(entry: Any) -> dict[str, Any]:
    if isinstance(entry, str):
        try:
            parsed = json.loads(entry)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            entry = parsed
        else:
            return {"description": entry, "reason_rejected": None, "ref": None}
    if isinstance(entry, dict):
        return {
            "description": entry.get("description") or "",
            "reason_rejected": entry.get("reason_rejected"),
            "ref": entry.get("ref"),
        }
    return {"description": str(entry), "reason_rejected": None, "ref": None}


def normalize_verdict_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list):
        if not payload:
            raise LLMError("LLM verdict JSON was an empty list")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise LLMError("LLM verdict JSON was not an object")
    verdict = payload.get("verdict")
    if isinstance(verdict, list):
        verdict = verdict[0] if verdict else None
    if not isinstance(verdict, str):
        raise LLMError(f"LLM verdict missing or not a string: {verdict!r}")
    verdict = verdict.strip().lower()
    if verdict not in {"contradicts", "related", "unrelated"}:
        raise LLMError(f"LLM verdict not in contradicts|related|unrelated: {verdict}")
    reason = payload.get("reason") or ""
    if isinstance(reason, list):
        reason = " ".join(str(part) for part in reason)
    hunk = payload.get("hunk_ref") or ""
    if isinstance(hunk, list):
        hunk = hunk[0] if hunk else ""
    return {"verdict": verdict, "reason": str(reason).strip(), "hunk_ref": str(hunk).strip()}


def build_chat_body(system: str, user: str, model: str | None = None) -> dict[str, Any]:
    """Body of a chat completion. ``response_format`` is json_object, never json_schema."""
    return {
        "model": model or llm_model(),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }


def chat_json(system: str, user: str, timeout: float = 60.0) -> dict[str, Any]:
    key = llm_api_key()
    if not key:
        raise LLMError("DEEPSEEK_API_KEY is not set")
    model = llm_model()
    url = llm_base_url() + "/chat/completions"
    body = build_chat_body(system, user, model=model)
    if body["model"] != model:
        raise LLMError("refusing to send a model id other than LLM_MODEL")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.error("LLM request failed model=%s error=%s", model, exc)
        raise LLMError(f"LLM request failed: {exc}") from exc
    if response.status_code >= 400:
        logger.error("LLM HTTP %s model=%s body=%s", response.status_code, model, response.text[:500])
        raise LLMError(f"LLM HTTP {response.status_code}: {response.text[:500]}")
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        logger.error("LLM response shape invalid: %s", exc)
        raise LLMError(f"LLM response shape invalid: {exc}") from exc
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("LLM content was not JSON: %s", content[:300])
        raise LLMError(f"LLM content was not JSON: {exc}") from exc
    if not isinstance(parsed, (dict, list)):
        raise LLMError("LLM JSON root was not an object or list")
    return parsed
