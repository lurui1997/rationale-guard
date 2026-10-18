"""Small GitHub REST client. Public repositories only. Responses are cached on disk."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from rationale.settings import github_token, repo_root

logger = logging.getLogger(__name__)

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, cache_dir: Path | None = None, token: str | None = None, max_requests: int = 150):
        self.cache_dir = cache_dir or (repo_root() / "data" / "cache" / "github")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.token = token if token is not None else github_token()
        self.max_requests = max_requests
        self.requests = 0
        # GitHub answers 301/302 for renamed repos and for pull/*.diff. Follow them.
        self._client = httpx.Client(timeout=30.0, headers=self._headers(), follow_redirects=True)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "rationale-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def close(self) -> None:
        self._client.close()

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "__")
        return self.cache_dir / safe

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = path.strip("/") + ".json"
        if params:
            suffix = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            cache_key = path.strip("/") + "__" + suffix + ".json"
        cached = self._cache_path(cache_key)
        if cached.exists():
            return json.loads(cached.read_text())
        if self.requests >= self.max_requests:
            raise GitHubError(f"GitHub request budget exhausted ({self.max_requests})")
        url = path if path.startswith("http") else API + path
        delay = 1.0
        last_error = "unknown"
        for _attempt in range(4):
            self.requests += 1
            response = self._client.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(data))
                return data
            if response.status_code == 404:
                raise GitHubError(f"GitHub 404 for {path}. Private repositories are not ingested.")
            if response.status_code in {403, 429, 502, 503}:
                last_error = f"HTTP {response.status_code}: {response.text[:180]}"
                logger.warning("GitHub retry %s", last_error)
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise GitHubError(f"GitHub HTTP {response.status_code} for {path}: {response.text[:300]}")
        raise GitHubError(last_error)

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        return self.get_json(f"/repos/{repo}/pulls/{number}")

    def issue(self, repo: str, number: int) -> dict[str, Any]:
        return self.get_json(f"/repos/{repo}/issues/{number}")

    def pull_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        data = self.get_json(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100})
        if isinstance(data, dict):
            raise GitHubError(data.get("message") or "unexpected files payload")
        return data

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        data = self.get_json(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 50})
        return data if isinstance(data, list) else []

    def review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        data = self.get_json(f"/repos/{repo}/pulls/{number}/comments", {"per_page": 30})
        return data if isinstance(data, list) else []

    def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        data = self.get_json(f"/repos/{repo}/issues/{number}/comments", {"per_page": 30})
        return data if isinstance(data, list) else []

    def public_events(self, login: str) -> list[dict[str, Any]]:
        data = self.get_json(f"/users/{login}/events/public", {"per_page": 5})
        return data if isinstance(data, list) else []

    def pull_diff(self, repo: str, number: int) -> str:
        return fetch_pr_diff(repo, number)

    def commits_for_path(self, repo: str, path: str) -> list[dict[str, Any]]:
        encoded = quote(path, safe="/")
        data = self.get_json(f"/repos/{repo}/commits?path={encoded}&per_page=20")
        # get_json cache key uses path string; params empty. The path is in the URL.
        return data if isinstance(data, list) else []

    def raw_file(self, repo: str, path: str, ref: str = "master") -> str:
        cache = self._cache_path(f"raw__{repo.replace('/', '__')}__{ref}__{path.replace('/', '__')}.txt")
        if cache.exists():
            return cache.read_text()
        url = f"{RAW}/{repo}/{ref}/{path}"
        response = self._client.get(url)
        if response.status_code == 404 and ref == "master":
            response = self._client.get(f"{RAW}/{repo}/main/{path}")
        if response.status_code != 200:
            raise GitHubError(f"Raw file HTTP {response.status_code} for {path}")
        cache.write_text(response.text)
        return response.text


def fetch_pr_diff(repo: str, number: int, timeout: float = 30) -> str:
    """Download a public PR diff. ``*.diff`` URLs redirect; an unfollowed 302 is an empty pass."""
    url = f"https://github.com/{repo}/pull/{number}.diff"
    headers = {"User-Agent": "rationale-guard", "Accept": "text/plain"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
    if response.status_code != 200:
        raise GitHubError(f"Could not fetch PR diff: HTTP {response.status_code}")
    text = response.text or ""
    if not text.strip():
        raise GitHubError(f"PR diff for {repo}#{number} was empty after redirects")
    return text
