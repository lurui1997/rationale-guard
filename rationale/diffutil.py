"""Unified diff parsing. Paths and added/removed lines drive the guard; the LLM never sees a path the parser did not return."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|--)")


@dataclass
class FileDiff:
    path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def added_text(self) -> str:
        return "\n".join(self.added)

    def removed_text(self) -> str:
        return "\n".join(self.removed)


@dataclass
class ParsedDiff:
    files: list[FileDiff]

    @property
    def paths(self) -> list[str]:
        return [item.path for item in self.files if item.path]

    def symbols(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for item in self.files:
            for line in item.added + item.removed:
                for token in IDENT.findall(line):
                    lowered = token.lower()
                    if len(lowered) < 4 or lowered in seen:
                        continue
                    seen.add(lowered)
                    found.append(lowered)
        return found[:80]

    def only_docs_or_comments(self) -> bool:
        if not self.files:
            return False
        for item in self.files:
            if not item.path.endswith((".md", ".txt", ".rst")):
                meaningful = [
                    line
                    for line in item.added + item.removed
                    if line.strip() and not COMMENT_LINE.match(line)
                ]
                if meaningful:
                    return False
        return True


def parse_diff(diff: str) -> ParsedDiff:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            match = re.search(r" b/(.+)$", line)
            current = FileDiff(path=match.group(1).strip() if match else "")
            files.append(current)
            continue
        if line.startswith("+++ "):
            match = re.match(r"\+\+\+ (?:b/)?(.+)$", line)
            if current is not None and match and match.group(1) != "/dev/null":
                current.path = match.group(1).strip()
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current.added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            current.removed.append(line[1:])
    return ParsedDiff(files=[item for item in files if item.path])
