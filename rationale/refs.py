"""Deterministic reference and PR-template parsing.

Closing keywords inside unchecked template checklist lines are ignored.
A closing keyword must be followed by an issue number. Cross-repo refs are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TEMPLATE_LINE = re.compile(r"(?im)^\s*[-*]\s*\[[ xX]?\]\s.*$")
CLOSING = re.compile(
    r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\b\s*:?\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)\b"
)
ISSUE_REF = re.compile(r"(?<![\w/])#(?P<num>\d+)\b")
URL_REF = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<num>\d+)"
)
DEPENDS = re.compile(
    r"(?i)\b(?:depends on|stacked on|based on|builds on|requires|follow[- ]?up (?:to|of))\b"
    r"[^\n#]{0,80}#(?P<num>\d+)"
)
SUPERSEDE = re.compile(
    r"(?i)\b(?:supersedes|replaces|alternative to)\b[^\n#]{0,80}#(?P<num>\d+)"
)
EXTERNAL = re.compile(r"\bPAP-\d{1,5}\b")
COMMIT_PR = re.compile(r"\(#(?P<num>\d+)\)\s*$")
HEADING = re.compile(r"(?m)^(#{2,3})\s+(.+?)\s*$")

SECTION_MAP = {
    "thinking path": "thinking_path",
    "linked issues": "linked_issues",
    "linked issues or issue description": "linked_issues",
    "what changed": "what_changed",
    "verification": "verification",
    "risks": "risks",
    "model used": "model_used",
}


@dataclass
class Ref:
    number: int
    kind: str
    evidence: str
    via: str


@dataclass
class ParsedBody:
    sections: dict[str, str] = field(default_factory=dict)
    body_excerpt: str = ""
    fixes: list[Ref] = field(default_factory=list)
    references: list[Ref] = field(default_factory=list)
    supersedes: list[Ref] = field(default_factory=list)
    external_tickets: list[str] = field(default_factory=list)


def _norm_heading(title: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", "", title.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def strip_templates(text: str) -> str:
    no_comments = HTML_COMMENT.sub("", text or "")
    kept = [line for line in no_comments.splitlines() if not TEMPLATE_LINE.match(line)]
    return "\n".join(kept)


def split_sections(text: str) -> dict[str, str]:
    matches = list(HEADING.finditer(text or ""))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = SECTION_MAP.get(_norm_heading(match.group(2)))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if key and key not in sections:
            sections[key] = text[start:end].strip()
    return sections


def _line_at(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip()[:200]


def parse_refs(text: str, repo: str) -> ParsedBody:
    raw = text or ""
    sections = split_sections(raw)
    scanned = strip_templates(raw)
    parsed = ParsedBody(sections=sections, body_excerpt=scanned[:4000])
    seen_fix: set[int] = set()
    seen_ref: set[tuple[int, str]] = set()

    for match in CLOSING.finditer(scanned):
        mentioned = match.group("repo")
        if mentioned and mentioned.lower() != repo.lower():
            continue
        number = int(match.group("num"))
        if number in seen_fix:
            continue
        seen_fix.add(number)
        parsed.fixes.append(Ref(number, "fixes", _line_at(scanned, match.start()), "closing_keyword"))

    for match in DEPENDS.finditer(scanned):
        number = int(match.group("num"))
        key = (number, "depends_on")
        if key in seen_ref:
            continue
        seen_ref.add(key)
        parsed.references.append(Ref(number, "depends_on", _line_at(scanned, match.start()), "depends"))

    for match in SUPERSEDE.finditer(scanned):
        number = int(match.group("num"))
        parsed.supersedes.append(Ref(number, "supersedes", _line_at(scanned, match.start()), "supersede"))

    for match in ISSUE_REF.finditer(scanned):
        number = int(match.group("num"))
        if number in seen_fix:
            continue
        key = (number, "mention")
        if key in seen_ref:
            continue
        seen_ref.add(key)
        parsed.references.append(Ref(number, "mention", _line_at(scanned, match.start()), "mention"))

    owner, name = repo.split("/", 1)
    for match in URL_REF.finditer(scanned):
        if match.group("owner").lower() != owner.lower() or match.group("repo").lower() != name.lower():
            continue
        number = int(match.group("num"))
        key = (number, "mention")
        if number in seen_fix or key in seen_ref:
            continue
        seen_ref.add(key)
        parsed.references.append(Ref(number, "mention", _line_at(scanned, match.start()), "url"))

    parsed.external_tickets = sorted(set(EXTERNAL.findall(scanned)))
    return parsed


def commit_pr_number(subject: str) -> int | None:
    match = COMMIT_PR.search(subject or "")
    if not match:
        return None
    return int(match.group("num"))


def is_bot(login: str | None, typename: str | None = None, denylist: set[str] | None = None) -> bool:
    if not login:
        return True
    if typename == "Bot":
        return True
    if login.endswith("[bot]"):
        return True
    if denylist and login.lower() in {item.lower() for item in denylist}:
        return True
    return False
