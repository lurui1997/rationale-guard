"""Premise checks. Each rule is one parameterized Cypher query."""

from __future__ import annotations

from typing import Any

from rationale.db import Store
from rationale.settings import inactive_days

# severity: high and warning make a premise stale. info means the premise still holds
# and is worth showing (an open linked issue is still the reason the decision exists).
RULES: list[dict[str, Any]] = [
    {
        "id": "R1_fixed_issue_still_open",
        "severity": "warning",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(pr:PR {state: 'MERGED'})-[f:FIXES]->(i:Issue {state: 'OPEN'})
            WHERE d.id IN $ids
            RETURN d.id AS decision_id, pr.id AS left_id, i.id AS right_id,
                   'merged ' + pr.id + ' claims to fix issue #' + toString(i.number) + ' which is still open' AS detail
        """,
    },
    {
        "id": "R1_linked_issue_still_open",
        "severity": "info",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(pr:PR)-[r:REFERENCES]->(i:Issue {state: 'OPEN'})
            WHERE d.id IN $ids
            RETURN d.id AS decision_id, pr.id AS left_id, i.id AS right_id,
                   'linked issue #' + toString(i.number) + ' is still open' AS detail
        """,
    },
    {
        "id": "R2_source_deleted",
        "severity": "high",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(doc:Doc)
            WHERE d.id IN $ids AND doc.deleted_in IS NOT NULL AND doc.deleted_in <> ''
            RETURN d.id AS decision_id, doc.id AS left_id, doc.id AS right_id, doc.path AS detail
            UNION
            MATCH (d:Decision)-[:IMPLEMENTED_BY]->(:Commit)-[:TOUCHES]->(f:File {exists: false})
            WHERE d.id IN $ids AND (f.path CONTAINS '/doc/' OR f.path CONTAINS '/docs/' OR f.path ENDS WITH '.md')
            RETURN d.id AS decision_id, f.id AS left_id, f.id AS right_id, f.path AS detail
        """,
    },
    {
        "id": "R3_adr_not_accepted",
        "severity": "warning",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(doc:Doc)
            WHERE d.id IN $ids AND doc.kind = 'adr' AND (
                toLower(doc.status) CONTAINS 'pending'
                OR toLower(doc.status) CONTAINS 'proposed'
                OR toLower(doc.status) CONTAINS 'draft'
            )
            RETURN d.id AS decision_id, doc.id AS left_id, doc.id AS right_id, doc.status AS detail
        """,
    },
    {
        "id": "R4_superseded",
        "severity": "high",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(old:PR)<-[s:SUPERSEDES]-(newer:PR)
            WHERE d.id IN $ids
            RETURN d.id AS decision_id, old.id AS left_id, newer.id AS right_id,
                   newer.state + ' ' + coalesce(s.evidence, '') AS detail
            UNION
            MATCH (newer:Decision)-[:SUPERSEDES]->(d:Decision)
            WHERE d.id IN $ids
            RETURN d.id AS decision_id, d.id AS left_id, newer.id AS right_id, newer.summary AS detail
        """,
    },
    {
        "id": "R5_dependency_unmerged",
        "severity": "warning",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(pr:PR)-[r:REFERENCES]->(dep:PR {state: 'OPEN'})
            WHERE d.id IN $ids AND r.kind IN ['depends_on', 'stack_base']
            RETURN d.id AS decision_id, pr.id AS left_id, dep.id AS right_id, coalesce(r.evidence, '') AS detail
        """,
    },
    {
        "id": "R6_owner_inactive",
        "severity": "info",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(src)<-[:AUTHORED]-(p:Person {is_bot: false})
            WHERE d.id IN $ids AND p.last_active > 0 AND p.last_active < $cutoff
            RETURN d.id AS decision_id, src.id AS left_id, p.id AS right_id,
                   p.login + ' last active ' + toString(p.last_active) AS detail
        """,
    },
    {
        "id": "R6_author_left_area",
        "severity": "info",
        "cypher": """
            MATCH (d:Decision)-[:DERIVED_FROM]->(src)<-[:AUTHORED]-(p:Person {is_bot: false})
            MATCH (src)-[:IN_AREA]->(a:Area)<-[:IN_AREA]-(later:PR)<-[:AUTHORED]-(other:Person {is_bot: false})
            WHERE d.id IN $ids AND other.id <> p.id
              AND coalesce(later.merged_at, later.created_at, 0) > coalesce(p.last_active, 0)
            WITH d, p, a, src, max(coalesce(later.merged_at, later.created_at, 0)) AS latest_other
            MATCH (p)-[:AUTHORED]->(own:PR)-[:IN_AREA]->(a)
            WITH d, p, a, src, latest_other, max(coalesce(own.merged_at, own.created_at, 0)) AS latest_own
            WHERE latest_own < latest_other
            RETURN d.id AS decision_id, p.id AS left_id, a.id AS right_id,
                   p.login + ' last touched ' + a.slug + ' before another author' AS detail
        """,
    },
    {
        "id": "R7_reverted_code",
        "severity": "high",
        "cypher": """
            MATCH (d:Decision)-[:IMPLEMENTED_BY]->(c:Commit)<-[:REVERTS]-(rev:Commit)
            WHERE d.id IN $ids
            RETURN d.id AS decision_id, c.id AS left_id, rev.id AS right_id,
                   'reverted by ' + rev.short_sha AS detail
        """,
    },
]


def check_premises(store: Store, graph: str, decision_ids: list[str], now: int | None = None) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    import time

    if not decision_ids:
        return {}, []
    cutoff = int(now if now is not None else time.time()) - inactive_days() * 86400
    grouped: dict[str, list[dict[str, Any]]] = {decision_id: [] for decision_id in decision_ids}
    traces: list[dict[str, Any]] = []
    params = {"ids": decision_ids, "cutoff": cutoff}
    for rule in RULES:
        rows, trace = store.query(graph, rule["cypher"], params)
        trace["name"] = rule["id"]
        traces.append(trace)
        for row in rows:
            decision_id = row.get("decision_id")
            if decision_id not in grouped:
                continue
            grouped[decision_id].append(
                {
                    "id": rule["id"],
                    "severity": rule["severity"],
                    "detail": row.get("detail") or "",
                    "evidence": [item for item in (row.get("left_id"), row.get("right_id")) if item],
                }
            )
    summary: dict[str, dict[str, Any]] = {}
    for decision_id, hits in grouped.items():
        stale = [hit for hit in hits if hit["severity"] in {"warning", "high"}]
        summary[decision_id] = {
            "status": "stale" if stale else "holds",
            "rules": hits,
        }
    return summary, traces
