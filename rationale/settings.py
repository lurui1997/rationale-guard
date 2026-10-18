"""Environment configuration. Model ids and graph URLs stay explicit."""

from __future__ import annotations

import os
from pathlib import Path

INTERNAL_MODEL_TAGS = frozenset(
    {
        "offline",
        "heuristic",
        "graph",
        "llm",
        "llm_error",
        "judge",
        "auto",
        "decision-extract-v1",
        "guard",
    }
)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "rationale").exists():
            return candidate
    return Path.cwd()


def llm_model() -> str:
    """Provider model id. Internal pipeline tags are rejected before any request."""
    model = env("LLM_MODEL", "deepseek-chat") or "deepseek-chat"
    lowered = model.lower()
    if lowered in INTERNAL_MODEL_TAGS or lowered.startswith("decision-") or lowered.startswith("judge-"):
        raise ValueError(
            f"LLM_MODEL must be a provider model id (default deepseek-chat), not an internal tag: {model}"
        )
    return model


def llm_api_key() -> str | None:
    return env("DEEPSEEK_API_KEY") or env("LLM_API_KEY")


def llm_base_url() -> str:
    return (env("LLM_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/")


def guard_judge_mode() -> str:
    mode = (env("GUARD_JUDGE", "auto") or "auto").lower()
    if mode not in {"auto", "offline", "llm"}:
        raise ValueError("GUARD_JUDGE must be auto, offline, or llm")
    return mode


def inactive_days() -> int:
    raw = env("STALE_INACTIVE_DAYS", "90") or "90"
    return int(raw)


def github_token() -> str | None:
    return env("GITHUB_TOKEN")
