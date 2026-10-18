"""Indexes for a rationale graph. Statements are static; names are validated upstream."""

from __future__ import annotations

import logging

from rationale.db import GraphError, Store

logger = logging.getLogger(__name__)

INDEXES = [
    "CREATE INDEX FOR (n:Decision) ON (n.id)",
    "CREATE INDEX FOR (n:PR) ON (n.id)",
    "CREATE INDEX FOR (n:Issue) ON (n.id)",
    "CREATE INDEX FOR (n:File) ON (n.id)",
    "CREATE INDEX FOR (n:Person) ON (n.id)",
    "CREATE INDEX FOR (n:Premise) ON (n.id)",
    "CREATE INDEX FOR (n:Alternative) ON (n.id)",
    "CREATE INDEX FOR (n:Correction) ON (n.id)",
    "CREATE INDEX FOR (n:Commit) ON (n.id)",
    "CREATE INDEX FOR (n:Doc) ON (n.id)",
    "CREATE INDEX FOR (n:Area) ON (n.id)",
    "CREATE INDEX FOR (n:PR) ON (n.number)",
    "CREATE INDEX FOR (n:Issue) ON (n.number)",
    "CREATE INDEX FOR (n:File) ON (n.path)",
]

FULLTEXT = "CALL db.idx.fulltext.createNodeIndex('Decision', 'summary', 'rationale', 'source_quote')"


def _touch(store: Store, graph: str) -> None:
    store.query(graph, "CREATE (:__Touch {id: '__touch__'})", create=True)
    store.query(graph, "MATCH (n:__Touch {id: '__touch__'}) DELETE n", create=True)


def ensure_schema(store: Store, graph: str) -> list[str]:
    notes: list[str] = []
    _touch(store, graph)
    for statement in INDEXES:
        try:
            store.query(graph, statement, create=True)
            notes.append(statement)
        except GraphError as exc:
            message = str(exc).lower()
            if "already indexed" in message or "equivalent index" in message or "already exists" in message:
                continue
            logger.warning("Index statement failed: %s", exc)
            notes.append(f"skip: {exc}")
    try:
        store.query(graph, FULLTEXT, create=True)
        notes.append(FULLTEXT)
    except GraphError as exc:
        message = str(exc).lower()
        if any(token in message for token in ("already", "exists", "equivalent")):
            return notes
        logger.warning("Fulltext index not created: %s", exc)
        notes.append(f"fulltext-skip: {exc}")
    return notes
