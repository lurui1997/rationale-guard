"""Graph-backed explanation for one decision. Wording comes from the nodes, not from an ungrounded model call."""

from __future__ import annotations

from typing import Any

from rationale.db import Store
from rationale.experts import experts_for_paths
from rationale.premises import check_premises

DECISION_QUERY = """
MATCH (d:Decision {id: $id})
OPTIONAL MATCH (d)-[:DERIVED_FROM]->(src)
RETURN d.id AS id, d.summary AS summary, d.source_quote AS quote, d.source_url AS source_url,
       d.status AS status, d.rationale AS rationale, d.extractor AS extractor,
       src.id AS source_id, src.title AS source_title, src.url AS source_url_node,
       src.number AS source_number, labels(src) AS source_labels
"""

CHAIN_QUERY = """
MATCH (d:Decision {id: $id})-[:DERIVED_FROM]->(src)
OPTIONAL MATCH (src)-[rel:FIXES|REFERENCES]->(other)
RETURN src.id AS source_id, type(rel) AS rel, other.id AS other_id, other.title AS other_title,
       other.url AS other_url, other.state AS other_state, other.number AS other_number
"""

FILE_QUERY = """
MATCH (d:Decision {id: $id})-[:DERIVED_FROM]->(src)
OPTIONAL MATCH (src)-[:TOUCHES]->(direct:File)
RETURN collect(DISTINCT direct.path) AS direct_paths, [] AS via_paths
"""


def why(store: Store, graph: str, decision_id: str) -> dict[str, Any]:
    rows, decision_trace = store.query(graph, DECISION_QUERY, {"id": decision_id})
    decision_trace["name"] = "decision"
    if not rows:
        raise ValueError(f"unknown decision {decision_id}")
    row = rows[0]
    chain_rows, chain_trace = store.query(graph, CHAIN_QUERY, {"id": decision_id})
    chain_trace["name"] = "chain"
    file_rows, file_trace = store.query(graph, FILE_QUERY, {"id": decision_id})
    file_trace["name"] = "files"
    paths: list[str] = []
    if file_rows:
        for key in ("direct_paths", "via_paths"):
            for path in file_rows[0].get(key) or []:
                if path and path not in paths:
                    paths.append(path)
    premises, premise_traces = check_premises(store, graph, [decision_id])
    ask, expert_traces = experts_for_paths(store, graph, paths[:20])
    alts, alt_trace = store.query(
        graph,
        """
        MATCH (alt:Alternative)-[:REJECTED_FOR]->(d:Decision {id: $id})
        RETURN alt.description AS description, alt.reason_rejected AS reason, alt.ref AS ref
        """,
        {"id": decision_id},
    )
    alt_trace["name"] = "alternatives"
    premise = premises.get(decision_id, {"status": "holds", "rules": []})
    chain = []
    if row.get("source_id"):
        chain.append(
            {
                "id": row["source_id"],
                "title": row.get("source_title") or row["source_id"],
                "url": row.get("source_url_node") or row.get("source_url"),
                "number": row.get("source_number"),
            }
        )
    for item in chain_rows:
        if item.get("other_id"):
            chain.append(
                {
                    "id": item["other_id"],
                    "title": item.get("other_title") or item["other_id"],
                    "url": item.get("other_url"),
                    "state": item.get("other_state"),
                    "number": item.get("other_number"),
                    "via": item.get("rel"),
                }
            )
    conclusion = (
        f"{row.get('summary')} Source: {row.get('source_id') or 'unknown'}. "
        f"Premise {premise['status']}."
    )
    return {
        "decision_id": decision_id,
        "judge": "graph",
        "conclusion": conclusion,
        "summary": row.get("summary"),
        "quote": row.get("quote"),
        "source_url": row.get("source_url") or row.get("source_url_node"),
        "status": row.get("status"),
        "rationale": row.get("rationale"),
        "extractor": row.get("extractor"),
        "chain": chain,
        "rejected_alternatives": alts,
        "premise": premise,
        "ask": ask,
        "paths": paths,
        "cypher_trace": [decision_trace, chain_trace, file_trace, alt_trace, *premise_traces, *expert_traces],
    }
