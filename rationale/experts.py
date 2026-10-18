"""Who to ask. PageRank and betweenness read the Person collaboration graph.

``COLLABORATED`` edges are written when a snapshot is loaded. Lookups here do not MERGE."""

from __future__ import annotations

import logging
from typing import Any

from rationale.db import GraphError, Store

logger = logging.getLogger(__name__)

LOCAL_PEOPLE = """
MATCH (p:Person)-[:EDITED]->(f:File)
WHERE p.is_bot = false AND f.path IN $paths
RETURN DISTINCT p.login AS login, p.id AS id
"""

DEGREE = """
MATCH (p:Person)-[:EDITED]->(f:File)
WHERE p.is_bot = false AND f.path IN $paths
RETURN p.login AS login, count(DISTINCT f) AS score
ORDER BY score DESC
LIMIT 5
"""

PAGERANK = [
    "CALL algo.pageRank('Person', 'COLLABORATED') YIELD node, score RETURN node.login AS login, score",
    """
    CALL algo.pageRank({nodeLabels: ['Person'], relationshipTypes: ['COLLABORATED']})
    YIELD node, score
    RETURN node.login AS login, score
    """,
]

BETWEENNESS = [
    "CALL algo.betweenness('Person', 'COLLABORATED') YIELD node, score RETURN node.login AS login, score",
    """
    CALL algo.betweenness({nodeLabels: ['Person'], relationshipTypes: ['COLLABORATED']})
    YIELD node, score
    RETURN node.login AS login, score
    """,
]


def _try_calls(store: Store, graph: str, statements: list[str], name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    errors: list[str] = []
    for statement in statements:
        try:
            rows, trace = store.query(graph, statement)
        except GraphError as exc:
            errors.append(str(exc))
            logger.warning("%s procedure failed: %s", name, exc)
            continue
        trace["name"] = name
        trace["algorithm"] = name
        return rows, trace
    trace = {
        "name": name,
        "cypher": statements[-1].strip(),
        "params": {},
        "ms": 0,
        "error": "; ".join(errors) if errors else "no procedure",
        "algorithm": "unavailable",
    }
    return [], trace


def experts_for_paths(store: Store, graph: str, paths: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    traces: list[dict[str, Any]] = []
    if not paths:
        return [], traces
    local_rows, local_trace = store.query(graph, LOCAL_PEOPLE, {"paths": paths})
    local_trace["name"] = "local_people"
    traces.append(local_trace)
    local = {row["login"] for row in local_rows if row.get("login")}

    ranked, pagerank_trace = _try_calls(store, graph, PAGERANK, "pagerank")
    traces.append(pagerank_trace)
    between, between_trace = _try_calls(store, graph, BETWEENNESS, "betweenness")
    traces.append(between_trace)
    between_scores = {row["login"]: row.get("score") for row in between if row.get("login")}

    algorithm = "pagerank"
    chosen = [row for row in ranked if row.get("login") in local]
    if not chosen:
        degree_rows, degree_trace = store.query(graph, DEGREE, {"paths": paths})
        degree_trace["name"] = "degree"
        degree_trace["algorithm"] = "degree"
        traces.append(degree_trace)
        chosen = degree_rows
        algorithm = "degree"
    else:
        algorithm = "pagerank" if not pagerank_trace.get("error") else "degree"

    people = []
    for row in chosen:
        login = row.get("login")
        if not login:
            continue
        people.append(
            {
                "login": login,
                "score": row.get("score") or 0,
                "algorithm": algorithm,
                "betweenness": between_scores.get(login),
            }
        )
    people.sort(key=lambda item: item["score"] or 0, reverse=True)
    if len(local) == 1 and people:
        people[0]["bus_factor"] = 1
    return people[:3], traces
