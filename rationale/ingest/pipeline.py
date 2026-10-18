"""Build a snapshot from the public GitHub API, then load one graph per repository."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import yaml

from rationale.ingest.extract import extract_decisions
from rationale.ingest.github_api import GitHubClient, GitHubError
from rationale.refs import commit_pr_number, is_bot, parse_refs
from rationale.settings import repo_root

logger = logging.getLogger(__name__)

Progress = Callable[[dict[str, Any]], None]
PR_URL = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
ADR_STATUS = re.compile(r"(?im)^status:\s*(.+)$")


def load_areas(path: str | None = None) -> dict[str, Any]:
    target = path or str(repo_root() / "config" / "areas.yaml")
    return yaml.safe_load(open(target))


def load_bots() -> set[str]:
    path = repo_root() / "config" / "bots.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return set(data.get("logins") or [])


def parse_pr_url(url: str) -> tuple[str, int]:
    match = PR_URL.search(url or "")
    if not match:
        raise ValueError("PR URL must look like https://github.com/owner/repo/pull/123")
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("number"))


def epoch(value: str | None) -> int | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp())


def pr_state(payload: dict[str, Any]) -> str:
    if payload.get("merged_at"):
        return "MERGED"
    state = (payload.get("state") or "open").upper()
    return "OPEN" if state == "OPEN" else "CLOSED"


def _area_for_number(config: dict[str, Any], number: int, text: str) -> str | None:
    lowered = text.lower()
    areas = config.get("areas") or []
    for area in areas:
        seeds = area.get("seeds") or {}
        if number in (seeds.get("prs") or []) or number in (seeds.get("issues") or []):
            return area["slug"]
    for area in areas:
        if any(keyword.lower() in lowered for keyword in area.get("keywords") or []):
            return area["slug"]
    return None


def _select_files(files: list[dict[str, Any]], keywords: list[str], limit: int) -> list[dict[str, Any]]:
    def rank(item: dict[str, Any]) -> float:
        path = item.get("filename") or ""
        lowered = path.lower()
        score = 0.0
        if any(keyword.lower() in lowered for keyword in keywords):
            score += 5
        if "/dist/" in lowered or lowered.endswith("lock.json") or "/package-lock" in lowered:
            score -= 6
        if ".test." in lowered or ".spec." in lowered or "/__tests__/" in lowered:
            score -= 0.5
        score += min(int(item.get("additions") or 0), 80) / 80
        return score

    ranked = sorted(files, key=rank, reverse=True)
    chosen = []
    for item in ranked:
        path = item.get("filename")
        if not path or path in {entry["path"] for entry in chosen}:
            continue
        chosen.append(
            {
                "path": path,
                "change": {"added": "A", "removed": "D", "renamed": "R", "modified": "M"}.get(item.get("status") or "", "M"),
                "additions": int(item.get("additions") or 0),
                "deletions": int(item.get("deletions") or 0),
            }
        )
        if len(chosen) >= limit:
            break
    return chosen


def _keywords(config: dict[str, Any]) -> list[str]:
    words: list[str] = []
    for area in config.get("areas") or []:
        words.extend(area.get("keywords") or [])
    return words


def _comment_texts(client: GitHubClient, repo: str, number: int, review_lines: list[str], include_reviews: bool = True) -> str:
    """Review bodies plus line comments and issue comments. Bots are skipped. Failures are logged."""
    bots = load_bots()
    chunks = [line for line in review_lines if line.strip()]
    if include_reviews:
        try:
            for comment in client.review_comments(repo, number):
                login = ((comment.get("user") or {}).get("login"))
                if is_bot(login, (comment.get("user") or {}).get("type"), bots):
                    continue
                text = (comment.get("body") or "").strip()
                if text:
                    chunks.append(text)
        except GitHubError as exc:
            logger.warning("No review comments for #%s: %s", number, exc)
    try:
        for comment in client.issue_comments(repo, number):
            login = ((comment.get("user") or {}).get("login"))
            if is_bot(login, (comment.get("user") or {}).get("type"), bots):
                continue
            text = (comment.get("body") or "").strip()
            if text:
                chunks.append(text)
    except GitHubError as exc:
        logger.warning("No issue comments for #%s: %s", number, exc)
    return "\n\n".join(chunks)[:12000]


def _discussion(client: GitHubClient, repo: str, number: int, review_lines: list[str]) -> str:
    return _comment_texts(client, repo, number, review_lines)


def _refresh_activity(client: GitHubClient, persons: dict[str, dict[str, Any]]) -> None:
    """Recent public events are evidence. A miss stays 'unknown' and must not be read as inactive."""
    checked = 0
    for person in persons.values():
        if person.get("is_bot"):
            person["activity_evidence"] = "unknown"
            continue
        if checked >= 12:
            person.setdefault("activity_evidence", "unknown")
            continue
        checked += 1
        try:
            events = client.public_events(person["login"])
        except GitHubError as exc:
            logger.warning("No public events for %s: %s", person["login"], exc)
            person["activity_evidence"] = "unknown"
            continue
        person["activity_evidence"] = "github"
        newest = 0
        for event in events:
            newest = max(newest, int(epoch(event.get("created_at")) or 0))
        if newest:
            person["last_active"] = max(int(person.get("last_active") or 0), newest)
        elif not events:
            # GitHub only lists recent public events. An empty page is evidence, not a guess.
            person["last_active"] = 0


def _emit(progress: Progress | None, **payload: Any) -> None:
    if progress:
        progress(payload)


def snapshot_from_seeds(
    config: dict[str, Any] | None = None,
    mode: str = "offline",
    client: GitHubClient | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    config = config or load_areas()
    own_client = client is None
    client = client or GitHubClient()
    try:
        return _snapshot_from_seeds(config, mode, client, progress)
    finally:
        if own_client:
            client.close()


def _snapshot_from_seeds(config: dict[str, Any], mode: str, client: GitHubClient, progress: Progress | None) -> dict[str, Any]:
    repo = config["repo"]
    bots = load_bots()
    pr_numbers: list[int] = []
    issue_numbers: list[int] = []
    docs: list[tuple[str, str]] = []
    for area in config.get("areas") or []:
        seeds = area.get("seeds") or {}
        for number in seeds.get("prs") or []:
            if number not in pr_numbers:
                pr_numbers.append(int(number))
        for number in seeds.get("issues") or []:
            if number not in issue_numbers:
                issue_numbers.append(int(number))
        for path in seeds.get("docs") or []:
            docs.append((area["slug"], path))
    pr_numbers = pr_numbers[: int(config.get("max_prs") or 30)]
    return _assemble(
        repo=repo,
        config=config,
        client=client,
        pr_numbers=pr_numbers,
        issue_numbers=issue_numbers,
        docs=docs,
        mode=mode,
        bots=bots,
        progress=progress,
    )


def snapshot_from_pr(
    pr_url: str,
    mode: str = "auto",
    client: GitHubClient | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    repo, number = parse_pr_url(pr_url)
    own_client = client is None
    client = client or GitHubClient()
    try:
        _emit(progress, step="fetch_pr", detail=f"{repo}#{number}")
        payload = client.pull(repo, number)
        canonical = (((payload.get("base") or {}).get("repo") or {}).get("full_name")) or repo
        if canonical and canonical.lower() != repo.lower():
            logger.info("PR URL %s resolved to %s after redirect", repo, canonical)
            repo = canonical
        if payload.get("private"):
            raise GitHubError("Private repositories are not ingested")
        files = client.pull_files(repo, number)[:20]
        paths = [item["filename"] for item in files if item.get("filename")][:20]
        numbers = [number]
        cutoff = datetime.now(timezone.utc) - timedelta(days=540)
        for path in paths:
            if len(numbers) >= 30:
                break
            _emit(progress, step="history", detail=path, prs=len(numbers))
            try:
                commits = client.commits_for_path(repo, path)
            except GitHubError as exc:
                logger.warning("Skipping history for %s: %s", path, exc)
                continue
            for commit in commits:
                committed = ((commit.get("commit") or {}).get("committer") or {}).get("date")
                when = epoch(committed) or 0
                if when and when < int(cutoff.timestamp()):
                    continue
                message = ((commit.get("commit") or {}).get("message") or "").splitlines()
                subject = message[0] if message else ""
                found = commit_pr_number(subject)
                if found and found not in numbers:
                    numbers.append(found)
                if len(numbers) >= 30:
                    break
        config = {"repo": repo, "areas": [{"slug": "touched", "name": "Touched by the PR", "keywords": [], "seeds": {"prs": numbers}}], "max_files_per_pr": 20}
        return _assemble(
            repo=repo,
            config=config,
            client=client,
            pr_numbers=numbers,
            issue_numbers=[],
            docs=[],
            mode=mode,
            bots=load_bots(),
            progress=progress,
        )
    finally:
        if own_client:
            client.close()


def _assemble(
    repo: str,
    config: dict[str, Any],
    client: GitHubClient,
    pr_numbers: list[int],
    issue_numbers: list[int],
    docs: list[tuple[str, str]],
    mode: str,
    bots: set[str],
    progress: Progress | None,
) -> dict[str, Any]:
    persons: dict[str, dict[str, Any]] = {}
    prs: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    limit = int(config.get("max_files_per_pr") or 25)
    keywords = _keywords(config)
    linked_issue_numbers = list(issue_numbers)

    def remember_person(login: str | None, typename: str | None, when: int | None) -> None:
        if not login:
            return
        bot = is_bot(login, typename, bots)
        current = persons.get(login)
        stamp = int(when or 0)
        if current is None:
            persons[login] = {"login": login, "is_bot": bot, "last_active": stamp}
        else:
            current["last_active"] = max(current["last_active"], stamp)
            current["is_bot"] = current["is_bot"] or bot

    for index, number in enumerate(pr_numbers, start=1):
        _emit(progress, step="pull", detail=f"#{number}", index=index, total=len(pr_numbers))
        payload = client.pull(repo, number)
        body = payload.get("body") or ""
        author = (payload.get("user") or {}).get("login")
        typename = (payload.get("user") or {}).get("type")
        created = epoch(payload.get("created_at"))
        remember_person(author, typename, epoch(payload.get("updated_at")) or created)
        parsed = parse_refs(body, repo)
        area = _area_for_number(config, number, f"{payload.get('title') or ''}\n{body[:1500]}")
        try:
            file_payload = client.pull_files(repo, number)
        except GitHubError as exc:
            logger.warning("No files for #%s: %s", number, exc)
            file_payload = []
        files = _select_files(file_payload, keywords, limit)
        for ref in parsed.references + parsed.fixes:
            if ref.number not in pr_numbers and ref.number not in linked_issue_numbers:
                linked_issue_numbers.append(ref.number)
        record = {
            "number": number,
            "title": payload.get("title") or "",
            "state": pr_state(payload),
            "merged_at": epoch(payload.get("merged_at")),
            "closed_at": epoch(payload.get("closed_at")),
            "created_at": created,
            "author": author,
            "url": payload.get("html_url"),
            "body": body[:8000],
            "area": area,
            "merge_sha": ((payload.get("merge_commit_sha") or "") if payload.get("merged_at") else ""),
            "files": files,
            "fixes": [ref.__dict__ for ref in parsed.fixes],
            "references": [ref.__dict__ for ref in parsed.references],
            "supersedes": [ref.__dict__ for ref in parsed.supersedes],
        }
        prs.append(record)
        review_bodies: list[str] = []
        try:
            for review in client.reviews(repo, number):
                login = ((review.get("user") or {}).get("login"))
                remember_person(login, (review.get("user") or {}).get("type"), epoch(review.get("submitted_at")))
                body_text = (review.get("body") or "").strip()
                if body_text and not is_bot(login, (review.get("user") or {}).get("type"), bots):
                    review_bodies.append(body_text)
                if login:
                    reviews.append(
                        {
                            "pr": number,
                            "login": login,
                            "state": review.get("state") or "COMMENTED",
                            "at": epoch(review.get("submitted_at")),
                            "is_bot": is_bot(login, (review.get("user") or {}).get("type"), bots),
                        }
                    )
        except GitHubError as exc:
            logger.warning("No reviews for #%s: %s", number, exc)
        discussion = _discussion(client, repo, number, review_bodies)
        artifact = {
            "kind": "pr",
            "source_id": f"pr:{number}",
            "state": record["state"],
            "area": area,
            "title": record["title"],
            "url": record["url"],
            "text": body,
            "structured": bool(parsed.sections),
            "section": "body",
        }
        for decision in extract_decisions(artifact, mode=mode):
            decisions.append(decision)
        if discussion.strip():
            for decision in extract_decisions(
                {
                    "kind": "pr",
                    "source_id": f"pr:{number}",
                    "state": record["state"],
                    "area": area,
                    "title": record["title"],
                    "url": record["url"],
                    "text": discussion,
                    "structured": False,
                    "section": "comments",
                },
                mode=mode,
            ):
                decisions.append(decision)

    # Only seeded issues are fetched. Other numbers stay as edges to stubs so a
    # public ingest stays inside the GitHub request budget.
    for number in issue_numbers:
        if number in pr_numbers:
            continue
        _emit(progress, step="issue", detail=f"#{number}")
        try:
            payload = client.issue(repo, number)
        except GitHubError as exc:
            logger.warning("Skipping issue #%s: %s", number, exc)
            continue
        if "pull_request" in payload:
            continue
        body = payload.get("body") or ""
        author = (payload.get("user") or {}).get("login")
        remember_person(author, (payload.get("user") or {}).get("type"), epoch(payload.get("updated_at")))
        area = _area_for_number(config, number, f"{payload.get('title') or ''}\n{body[:1500]}")
        issues.append(
            {
                "number": number,
                "title": payload.get("title") or "",
                "state": (payload.get("state") or "open").upper(),
                "state_reason": payload.get("state_reason") or "",
                "created_at": epoch(payload.get("created_at")),
                "closed_at": epoch(payload.get("closed_at")),
                "author": author,
                "url": payload.get("html_url"),
                "body": body[:8000],
                "area": area,
            }
        )
        if number in issue_numbers:
            artifact = {
                "kind": "issue",
                "source_id": f"issue:{number}",
                "state": (payload.get("state") or "open").upper(),
                "area": area,
                "title": payload.get("title") or "",
                "url": payload.get("html_url"),
                "text": body,
                "structured": True,
                "section": "body",
            }
            for decision in extract_decisions(artifact, mode=mode):
                decisions.append(decision)
        issue_notes = _comment_texts(client, repo, number, review_lines=[], include_reviews=False)
        if issue_notes.strip():
            for decision in extract_decisions(
                {
                    "kind": "issue",
                    "source_id": f"issue:{number}",
                    "state": (payload.get("state") or "open").upper(),
                    "area": area,
                    "title": payload.get("title") or "",
                    "url": payload.get("html_url"),
                    "text": issue_notes,
                    "structured": False,
                    "section": "comments",
                },
                mode=mode,
            ):
                decisions.append(decision)

    _refresh_activity(client, persons)

    doc_nodes = []
    for area, path in docs:
        _emit(progress, step="doc", detail=path)
        text = client.raw_file(repo, path)
        status_match = ADR_STATUS.search(text)
        status = status_match.group(1).strip() if status_match else ""
        kind = "adr" if "adr" in path.lower() else "plan" if "/plans/" in path else "other"
        title = path.rsplit("/", 1)[-1]
        doc_nodes.append(
            {
                "path": path,
                "kind": kind,
                "title": title,
                "status": status,
                "area": area,
                "url": f"https://github.com/{repo}/blob/master/{path}",
            }
        )
        for pr in prs:
            for item in pr["files"]:
                if item["path"] == path:
                    item["doc"] = True
        artifact = {
            "kind": "doc",
            "source_id": f"doc:{path}",
            "state": "MERGED",
            "area": area,
            "title": title,
            "url": f"https://github.com/{repo}/blob/master/{path}",
            "text": text,
            "structured": True,
            "section": "adr_decision",
            "doc_status": status,
        }
        for decision in extract_decisions(artifact, mode=mode):
            decisions.append(decision)

    # Closed-unmerged PRs referenced as alternatives stay in the graph via the PR list.
    deduped: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        deduped[decision["id"]] = decision
    return {
        "repo": repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "extractor": mode,
        "areas": [
            {"slug": area["slug"], "name": area.get("name") or area["slug"]}
            for area in config.get("areas") or []
        ],
        "persons": list(persons.values()),
        "prs": prs,
        "issues": issues,
        "docs": doc_nodes,
        "reviews": reviews,
        "decisions": list(deduped.values()),
    }
