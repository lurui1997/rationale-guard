#!/usr/bin/env python3
"""GitHub Action helper.

Posts one sticky pull-request comment (marker ``<!-- rationale-guard -->``)
and a completed check run. Also accepts ``/rationale ack|override|wrong`` comments.

This script only talks to the repository in GITHUB_REPOSITORY. Do not point it
at a repository you do not own.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

MARKER = "<!-- rationale-guard -->"
API = "https://api.github.com"
COMMAND = re.compile(
    r"(?im)^/rationale\s+(ack|override|wrong)(?:\s+(decision:[0-9a-fA-F]+))?\b[:\s]*(.*)$"
)
WRITE_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or ""
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    event_name = os.environ.get("GITHUB_EVENT_NAME") or ""
    event = json.loads(open(os.environ["GITHUB_EVENT_PATH"]).read()) if os.environ.get("GITHUB_EVENT_PATH") else {}
    api_url = (os.environ.get("RATIONALE_API_URL") or "").rstrip("/")
    if not api_url:
        print("RATIONALE_API_URL is not set; skipping.", file=sys.stderr)
        return 0
    if not repository or "/" not in repository:
        print("GITHUB_REPOSITORY is required.", file=sys.stderr)
        return 1
    if event_name == "issue_comment":
        return handle_comment(api_url, token, repository, event)
    return handle_pull(api_url, token, repository, event)


def handle_comment(api_url: str, token: str, repository: str, event: dict) -> int:
    body = ((event.get("comment") or {}).get("body")) or ""
    match = COMMAND.search(body)
    if not match:
        print("Comment is not a /rationale command.")
        return 0
    issue = event.get("issue") or {}
    number = issue.get("number")
    if not number or "pull_request" not in issue:
        print("Command was not on a pull request.")
        return 0
    kind, decision_id, text = match.group(1), match.group(2), (match.group(3) or "").strip()
    if not text:
        print("A reason is required.", file=sys.stderr)
        return 1
    user = ((event.get("comment") or {}).get("user") or {}).get("login") or os.environ.get("GITHUB_ACTOR") or "unknown"
    association = ((event.get("comment") or {}).get("author_association")) or ""
    if not _can_write(token, repository, user, association):
        _comment(
            token,
            repository,
            int(number),
            f"{MARKER}\n\n`/rationale {kind}` was ignored. Only repository collaborators with write access can change the graph.",
        )
        return 0
    report = guard_pr(api_url, repository, int(number), ingest=True)
    decision_id = decision_id or _first_decision(report)
    if not decision_id:
        _comment(token, repository, int(number), f"{MARKER}\n\nNo guarded decision was available to attach this {kind} to.")
        return 0
    _post_json(
        f"{api_url}/api/feedback",
        {"repo": repository, "decision_id": decision_id, "kind": kind, "text": text, "by": user, "pr": number},
    )
    updated = guard_pr(api_url, repository, int(number), ingest=False)
    _upsert_comment(token, repository, int(number), render(updated))
    _check(token, repository, updated, event, int(number))
    return 0


def handle_pull(api_url: str, token: str, repository: str, event: dict) -> int:
    number = ((event.get("pull_request") or {}).get("number")) or event.get("number")
    if not number:
        print("No pull request number in the event.", file=sys.stderr)
        return 1
    report = guard_pr(api_url, repository, int(number), ingest=True)
    _upsert_comment(token, repository, int(number), render(report))
    _check(token, repository, report, event, int(number))
    return 0


def guard_pr(api_url: str, repository: str, number: int, ingest: bool = True) -> dict:
    return _post_json(
        f"{api_url}/api/guard",
        {
            "repo": repository,
            "pr_url": f"https://github.com/{repository}/pull/{number}",
            "ingest": ingest,
        },
    )


def render(report: dict) -> str:
    lines = [MARKER, ""]
    status = report.get("status")
    findings = report.get("findings") or []
    contradicts = [item for item in findings if item.get("verdict") == "contradicts"]
    if status == "warn" and contradicts:
        lines.append(f"⚠️ **Rationale Guard** found {len(contradicts)} past decision(s) this change may contradict.")
    else:
        lines.append("✅ **Rationale Guard** did not find a contradiction with a past decision.")
    lines.append("")
    for finding in (contradicts or findings)[:2]:
        icon = "⚠️" if finding.get("verdict") == "contradicts" else "•"
        lines.append(f"{icon} **{finding.get('verdict')}** — {finding.get('summary', '')[:160]}")
        if finding.get("quote"):
            lines.append(f"> {finding['quote'][:220]}")
        premise = finding.get("premise") or {}
        rules = ", ".join(rule.get("id", "") for rule in (premise.get("rules") or [])[:3]) or "none"
        lines.append(f"Premise **{premise.get('status', 'holds')}** ({rules}).")
        alts = finding.get("rejected_alternatives") or []
        if alts:
            alt = alts[0]
            lines.append(f"Rejected alternative: {alt.get('description', '')[:140]}")
        ask = finding.get("ask") or []
        if ask:
            lines.append("Ask " + ", ".join(f"@{person['login']}" for person in ask[:3]))
        learned = finding.get("learned_from")
        if learned:
            lines.append(f"Learned from @{learned.get('by')}: {learned.get('kind')} — {learned.get('text', '')[:120]}")
    learned_rows = report.get("learned") or []
    if learned_rows and status != "warn":
        row = learned_rows[0]
        lines.append(f"Learned from @{row.get('by')}: {row.get('kind')} superseded {row.get('decision_id')}.")
    lines.append("")
    lines.append("Collaborators with write access can reply `/rationale ack|override|wrong <reason>`.")
    lines.append(f"Graph step: {report.get('graph_ms', '?')} ms. Judge: {report.get('judge', '?')}.")
    text = "\n".join(lines[:25])
    return text


def _first_decision(report: dict) -> str | None:
    findings = report.get("findings") or []
    contradicts = [item for item in findings if item.get("verdict") == "contradicts" and item.get("decision_id")]
    if contradicts:
        return contradicts[0]["decision_id"]
    if findings and findings[0].get("decision_id"):
        return findings[0]["decision_id"]
    learned = report.get("learned") or []
    if learned:
        return learned[0].get("decision_id")
    return None


def event_head_sha(event: dict) -> str | None:
    """PR-head SHA from the event. Issue comments do not carry it; do not substitute GITHUB_SHA."""
    head = ((event.get("pull_request") or {}).get("head") or {})
    if isinstance(head, dict) and head.get("sha"):
        return head["sha"]
    return None


def association_can_write(association: str) -> bool:
    return (association or "").upper() in WRITE_ASSOCIATIONS


def _can_write(token: str, repository: str, login: str, association: str) -> bool:
    if association_can_write(association):
        return True
    if not token or not login or login == "unknown":
        return False
    try:
        data = _request("GET", f"/repos/{repository}/collaborators/{login}/permission", token)
    except Exception as exc:
        print(f"Permission lookup failed for {login}: {exc}", file=sys.stderr)
        return False
    return (data.get("permission") or "") in {"admin", "maintain", "write"}


def _upsert_comment(token: str, repository: str, number: int, body: str) -> None:
    if not token:
        print(body)
        return
    comments = _request("GET", f"/repos/{repository}/issues/{number}/comments", token)
    existing = next((item for item in comments if MARKER in (item.get("body") or "")), None)
    if existing:
        _request("PATCH", f"/repos/{repository}/issues/comments/{existing['id']}", token, {"body": body})
    else:
        _request("POST", f"/repos/{repository}/issues/{number}/comments", token, {"body": body})


def _comment(token: str, repository: str, number: int, body: str) -> None:
    _upsert_comment(token, repository, number, body)


def _check(token: str, repository: str, report: dict, event: dict, number: int | None = None) -> None:
    sha = event_head_sha(event)
    if not sha and token and number:
        try:
            pr = _request("GET", f"/repos/{repository}/pulls/{number}", token)
            sha = ((pr.get("head") or {}).get("sha")) or None
        except Exception as exc:
            print(f"Could not read PR head SHA: {exc}", file=sys.stderr)
            sha = None
    if not token or not sha:
        return
    conclusion = "neutral"
    title = "Rationale Guard passed" if report.get("status") != "warn" else "Rationale Guard found a contradiction"
    _request(
        "POST",
        f"/repos/{repository}/check-runs",
        token,
        {
            "name": "Rationale Guard",
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": (report.get("summary") or title)[:600]},
        },
    )


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "rationale-guard"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def _request(method: str, path: str, token: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "rationale-guard",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read().decode()
        return json.loads(raw) if raw else {}


if __name__ == "__main__":
    raise SystemExit(main())
