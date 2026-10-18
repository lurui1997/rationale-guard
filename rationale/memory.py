"""Corrections are graph nodes. An override writes a superseding decision."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from rationale.db import Store
from rationale.ingest.load import ensure_node, relate, upsert

KINDS = {"ack", "override", "wrong"}


def record_correction(
    store: Store,
    graph: str,
    decision_id: str,
    kind: str,
    text: str,
    by: str,
    pr: int | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("kind must be ack, override, or wrong")
    if not text or not text.strip():
        raise ValueError("correction text is required")
    rows, _trace = store.query(
        graph,
        "MATCH (d:Decision {id: $id}) RETURN d.id AS id, d.summary AS summary, d.extracted_from AS extracted_from",
        {"id": decision_id},
    )
    if not rows:
        raise ValueError(f"unknown decision {decision_id}")
    now = int(time.time())
    digest = hashlib.sha1(f"{decision_id}|{kind}|{text}|{by}|{now}".encode()).hexdigest()[:12]
    correction_id = f"correction:{digest}"
    upsert(
        store,
        graph,
        "Correction",
        correction_id,
        {"kind": kind, "text": text.strip()[:500], "by": by or "unknown", "at": now, "pr": int(pr or 0)},
    )
    relate(store, graph, correction_id, "ABOUT", decision_id)
    if pr:
        ensure_node(store, graph, "PR", f"pr:{int(pr)}", {"number": int(pr), "state": "OPEN", "title": ""})
        relate(store, graph, correction_id, "ON_PR", f"pr:{int(pr)}")
    successor = None
    if kind == "override":
        successor = _supersede(store, graph, decision_id, text.strip(), by or "unknown", rows[0].get("extracted_from") or "")
    elif kind == "wrong":
        store.query(
            graph,
            "MATCH (d:Decision {id: $id}) SET d.wrong_count = coalesce(d.wrong_count, 0) + 1",
            {"id": decision_id},
        )
    return {
        "id": correction_id,
        "kind": kind,
        "text": text.strip(),
        "by": by or "unknown",
        "at": now,
        "pr": pr,
        "decision_id": decision_id,
        "successor_id": successor,
    }


def _supersede(store: Store, graph: str, decision_id: str, text: str, by: str, extracted_from: str) -> str:
    store.query(
        graph,
        "MATCH (d:Decision {id: $id}) SET d.status = 'superseded'",
        {"id": decision_id},
    )
    digest = hashlib.sha1(f"override\n{decision_id}\n{text}".encode()).hexdigest()[:12]
    successor = f"decision:{digest}"
    upsert(
        store,
        graph,
        "Decision",
        successor,
        {
            "summary": text[:200],
            "rationale": text[:600],
            "status": "accepted",
            "source_quote": text[:300],
            "source_url": "",
            "confidence": 1.0,
            "extractor": "correction",
            "model": "",
            "extracted_from": extracted_from,
            "section": "correction",
            "wrong_count": 0,
            "symbols": [],
        },
    )
    relate(store, graph, successor, "SUPERSEDES", decision_id, {"evidence": text[:200]})
    if extracted_from:
        relate(store, graph, successor, "DERIVED_FROM", extracted_from)
    return successor


def list_corrections(store: Store, graph: str, limit: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, trace = store.query(
        graph,
        """
        MATCH (c:Correction)-[:ABOUT]->(d:Decision)
        OPTIONAL MATCH (n:Decision)-[:SUPERSEDES]->(d)
        RETURN c.id AS id, c.kind AS kind, c.text AS text, c.by AS by, c.at AS at, c.pr AS pr,
               d.id AS decision_id, d.summary AS decision_summary, d.status AS decision_status,
               n.id AS successor_id, n.summary AS successor_summary
        ORDER BY c.at DESC
        LIMIT $limit
        """,
        {"limit": int(limit)},
    )
    trace["name"] = "learned"
    return rows, trace
