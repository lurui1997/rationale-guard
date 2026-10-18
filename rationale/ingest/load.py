"""Load a snapshot into one FalkorDB graph. Every write is a parameterized MERGE."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import json

from rationale.db import Store
from rationale.schema import ensure_schema

logger = logging.getLogger(__name__)

LABELS = {
    "Decision",
    "PR",
    "Issue",
    "File",
    "Person",
    "Premise",
    "Alternative",
    "Correction",
    "Commit",
    "Doc",
    "Area",
}
RELS = {
    "AUTHORED",
    "REVIEWED",
    "REFERENCES",
    "FIXES",
    "MERGED_AS",
    "TOUCHES",
    "ADDS",
    "IN_AREA",
    "IMPLEMENTED_BY",
    "DERIVED_FROM",
    "SUPERSEDES",
    "REJECTED_ALTERNATIVE",
    "REJECTED_FOR",
    "HAS_PREMISE",
    "REFERS_TO",
    "DESCRIBED_IN",
    "EDITED",
    "REVERTS",
    "COVERS",
    "ON_PR",
    "ABOUT",
    "RESULTED_IN",
    "COLLABORATED",
}
PROP = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, list):
        cleaned = []
        for item in value:
            if isinstance(item, (str, int, float, bool)):
                cleaned.append(item)
        return cleaned
    return str(value)


def upsert(store: Store, graph: str, label: str, node_id: str, props: dict[str, Any]) -> None:
    if label not in LABELS:
        raise ValueError(f"unknown label {label}")
    keys = [key for key in props if PROP.fullmatch(key) and _clean(props[key]) is not None]
    params: dict[str, Any] = {"id": node_id}
    assignments = ["n.id = $id"]
    for key in keys:
        params[key] = _clean(props[key])
        assignments.append(f"n.{key} = ${key}")
    cypher = f"MERGE (n:{label} {{id: $id}}) SET " + ", ".join(assignments)
    store.query(graph, cypher, params, create=True)


def ensure_node(store: Store, graph: str, label: str, node_id: str, props: dict[str, Any]) -> None:
    """Create a stub only when the id is new, so a later reference cannot blank a loaded node."""
    if label not in LABELS:
        raise ValueError(f"unknown label {label}")
    keys = [key for key in props if PROP.fullmatch(key) and _clean(props[key]) is not None]
    params: dict[str, Any] = {"id": node_id}
    assignments = ["n.id = $id"]
    for key in keys:
        params[key] = _clean(props[key])
        assignments.append(f"n.{key} = ${key}")
    cypher = f"MERGE (n:{label} {{id: $id}}) ON CREATE SET " + ", ".join(assignments)
    store.query(graph, cypher, params, create=True)


def relate(
    store: Store,
    graph: str,
    src: str,
    rel: str,
    dst: str,
    props: dict[str, Any] | None = None,
) -> None:
    if rel not in RELS:
        raise ValueError(f"unknown relationship {rel}")
    params: dict[str, Any] = {"src": src, "dst": dst}
    set_clause = ""
    if props:
        keys = [key for key in props if PROP.fullmatch(key) and _clean(props[key]) is not None]
        bits = []
        for key in keys:
            params[key] = _clean(props[key])
            bits.append(f"r.{key} = ${key}")
        if bits:
            set_clause = " SET " + ", ".join(bits)
    cypher = (
        f"MATCH (a {{id: $src}}), (b {{id: $dst}}) "
        f"MERGE (a)-[r:{rel}]->(b){set_clause}"
    )
    store.query(graph, cypher, params, create=True)


def path_symbols(path: str) -> list[str]:
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r"\.(test|spec)\.[^.]+$", "", name)
    name = re.sub(r"\.[^.]+$", "", name)
    parts = re.split(r"[^A-Za-z0-9]+", name)
    return [part.lower() for part in parts if len(part) >= 4][:8]


def load_snapshot(store: Store, snapshot: dict[str, Any], graph: str, reset: bool = False) -> dict[str, int]:
    if reset:
        store.delete_graph(graph)
    ensure_schema(store, graph)
    counts = {"nodes": 0, "edges": 0}
    areas = {area["slug"]: area for area in snapshot.get("areas") or []}
    for slug, area in areas.items():
        upsert(store, graph, "Area", f"area:{slug}", {"slug": slug, "name": area.get("name") or slug})
        counts["nodes"] += 1

    for person in snapshot.get("persons") or []:
        _upsert_person(store, graph, person)
        counts["nodes"] += 1

    pr_numbers = {int(pr["number"]) for pr in snapshot.get("prs") or []}
    issue_numbers = {int(issue["number"]) for issue in snapshot.get("issues") or []}

    for issue in snapshot.get("issues") or []:
        number = int(issue["number"])
        upsert(
            store,
            graph,
            "Issue",
            f"issue:{number}",
            {
                "number": number,
                "title": (issue.get("title") or "")[:300],
                "state": (issue.get("state") or "OPEN").upper(),
                "state_reason": issue.get("state_reason") or "",
                "created_at": int(issue.get("created_at") or 0),
                "closed_at": int(issue["closed_at"]) if issue.get("closed_at") else 0,
                "body_excerpt": (issue.get("body") or "")[:4000],
                "url": issue.get("url") or "",
            },
        )
        counts["nodes"] += 1
        if issue.get("author"):
            relate(store, graph, f"person:{issue['author']}", "AUTHORED", f"issue:{number}", {"at": int(issue.get("created_at") or 0)})
            counts["edges"] += 1
        if issue.get("area"):
            relate(store, graph, f"issue:{number}", "IN_AREA", f"area:{issue['area']}")
            counts["edges"] += 1

    for doc in snapshot.get("docs") or []:
        path = doc["path"]
        upsert(
            store,
            graph,
            "Doc",
            f"doc:{path}",
            {
                "path": path,
                "kind": doc.get("kind") or "other",
                "title": (doc.get("title") or path)[:200],
                "status": doc.get("status") or "",
                "url": doc.get("url") or "",
                "deleted_in": doc.get("deleted_in") or "",
            },
        )
        upsert(
            store,
            graph,
            "File",
            f"file:{path}",
            {"path": path, "exists": True, "symbols": path_symbols(path)},
        )
        counts["nodes"] += 2
        if doc.get("area"):
            relate(store, graph, f"doc:{path}", "IN_AREA", f"area:{doc['area']}")
            relate(store, graph, f"file:{path}", "IN_AREA", f"area:{doc['area']}")
            counts["edges"] += 2

    for pr in snapshot.get("prs") or []:
        number = int(pr["number"])
        upsert(
            store,
            graph,
            "PR",
            f"pr:{number}",
            {
                "number": number,
                "title": (pr.get("title") or "")[:300],
                "state": pr.get("state") or "OPEN",
                "merged_at": int(pr.get("merged_at") or 0),
                "closed_at": int(pr.get("closed_at") or 0),
                "created_at": int(pr.get("created_at") or 0),
                "url": pr.get("url") or "",
                "body_excerpt": (pr.get("body") or "")[:4000],
                "author": pr.get("author") or "",
            },
        )
        counts["nodes"] += 1
        if pr.get("author"):
            relate(
                store,
                graph,
                f"person:{pr['author']}",
                "AUTHORED",
                f"pr:{number}",
                {"at": int(pr.get("created_at") or 0)},
            )
            counts["edges"] += 1
        if pr.get("area"):
            relate(store, graph, f"pr:{number}", "IN_AREA", f"area:{pr['area']}")
            counts["edges"] += 1
        sha = pr.get("merge_sha")
        if sha:
            upsert(
                store,
                graph,
                "Commit",
                f"commit:{sha}",
                {"sha": sha, "short_sha": sha[:9], "date": int(pr.get("merged_at") or 0), "subject": (pr.get("title") or "")[:200]},
            )
            relate(store, graph, f"pr:{number}", "MERGED_AS", f"commit:{sha}")
            counts["nodes"] += 1
            counts["edges"] += 1
        for item in pr.get("files") or []:
            path = item["path"]
            upsert(
                store,
                graph,
                "File",
                f"file:{path}",
                {
                    "path": path,
                    "exists": item.get("change") != "D",
                    "symbols": path_symbols(path),
                },
            )
            relate(
                store,
                graph,
                f"pr:{number}",
                "TOUCHES",
                f"file:{path}",
                {"change": item.get("change") or "M", "additions": int(item.get("additions") or 0), "deletions": int(item.get("deletions") or 0)},
            )
            counts["edges"] += 1
            if sha:
                relate(store, graph, f"commit:{sha}", "TOUCHES", f"file:{path}", {"change": item.get("change") or "M"})
                counts["edges"] += 1
            if pr.get("author"):
                relate(store, graph, f"person:{pr['author']}", "EDITED", f"file:{path}")
                counts["edges"] += 1
            if pr.get("area"):
                relate(store, graph, f"file:{path}", "IN_AREA", f"area:{pr['area']}")
            if item.get("doc"):
                relate(store, graph, f"pr:{number}", "ADDS", f"doc:{path}")
                counts["edges"] += 1
        for ref in pr.get("fixes") or []:
            _link_ref(store, graph, number, ref, "FIXES", pr_numbers, issue_numbers)
            counts["edges"] += 1
        for ref in pr.get("references") or []:
            _link_ref(store, graph, number, ref, "REFERENCES", pr_numbers, issue_numbers)
            counts["edges"] += 1
        for ref in pr.get("supersedes") or []:
            _link_ref(store, graph, number, ref, "SUPERSEDES", pr_numbers, issue_numbers)
            counts["edges"] += 1

    for review in snapshot.get("reviews") or []:
        if not review.get("login"):
            continue
        relate(
            store,
            graph,
            f"person:{review['login']}",
            "REVIEWED",
            f"pr:{int(review['pr'])}",
            {"state": review.get("state") or "COMMENTED", "at": int(review.get("at") or 0)},
        )
        counts["edges"] += 1

    for decision in snapshot.get("decisions") or []:
        node_id = decision["id"]
        symbols = list(decision.get("symbols") or []) or _symbols_for_decision(snapshot, decision)
        _upsert_decision(
            store,
            graph,
            node_id,
            {
                "summary": (decision.get("summary") or "")[:200],
                "rationale": (decision.get("rationale") or "")[:600],
                "status": decision.get("status") or "accepted",
                "source_quote": (decision.get("source_quote") or "")[:300],
                "source_url": decision.get("source_url") or "",
                "confidence": float(decision.get("confidence") or 0),
                "extractor": decision.get("extractor") or "offline",
                "model": decision.get("model") or "",
                "extracted_from": decision.get("extracted_from") or "",
                "section": decision.get("section") or "",
                "wrong_count": int(decision.get("wrong_count") or 0),
                "symbols": symbols[:12],
            },
        )
        counts["nodes"] += 1
        source = decision.get("extracted_from") or ""
        if source:
            relate(store, graph, node_id, "DERIVED_FROM", source)
            counts["edges"] += 1
        if decision.get("area"):
            relate(store, graph, node_id, "IN_AREA", f"area:{decision['area']}")
            counts["edges"] += 1
        pr_number = _pr_number_from_source(source, decision)
        if pr_number and f"pr:{pr_number}" :
            sha = _merge_sha(snapshot, pr_number)
            if sha:
                relate(store, graph, node_id, "IMPLEMENTED_BY", f"commit:{sha}")
                counts["edges"] += 1
        for index, premise in enumerate(decision.get("premises") or []):
            premise_id = f"premise:{node_id}:{index}"
            upsert(
                store,
                graph,
                "Premise",
                premise_id,
                {"text": (premise.get("text") or "")[:300], "kind": "stated"},
            )
            relate(store, graph, node_id, "HAS_PREMISE", premise_id)
            counts["nodes"] += 1
            counts["edges"] += 1
            for issue_number in premise.get("issues") or []:
                issue_id = _id_for_number(int(issue_number), pr_numbers, issue_numbers)
                if issue_id.startswith("issue:"):
                    ensure_node(store, graph, "Issue", issue_id, {"number": int(issue_number), "state": "", "title": ""})
                    relate(store, graph, premise_id, "REFERS_TO", issue_id)
                    counts["edges"] += 1
        for index, alt in enumerate(decision.get("alternatives") or []):
            description = (alt.get("description") or "")[:300]
            if not description:
                continue
            alt_id = f"alternative:{node_id}:{index}"
            upsert(
                store,
                graph,
                "Alternative",
                alt_id,
                {
                    "description": description,
                    "reason_rejected": (alt.get("reason_rejected") or "")[:300],
                    "ref": alt.get("ref") or "",
                },
            )
            relate(store, graph, alt_id, "REJECTED_FOR", node_id, {"reason": (alt.get("reason_rejected") or "")[:200]})
            counts["nodes"] += 1
            counts["edges"] += 1
            ref = alt.get("ref") or ""
            match = re.fullmatch(r"#(\d+)", ref.strip())
            if match:
                target = _id_for_number(int(match.group(1)), pr_numbers, issue_numbers)
                relate(store, graph, alt_id, "DESCRIBED_IN", target)
                if target.startswith("pr:"):
                    relate(
                        store,
                        graph,
                        target,
                        "REJECTED_ALTERNATIVE",
                        node_id,
                        {"reason": (alt.get("reason_rejected") or "")[:200], "evidence": description[:200]},
                    )
                    counts["edges"] += 1
        for ref in decision.get("supersedes") or []:
            match = re.fullmatch(r"#(\d+)", str(ref).strip())
            if not match or not source:
                continue
            target = _id_for_number(int(match.group(1)), pr_numbers, issue_numbers)
            relate(store, graph, source, "SUPERSEDES", target, {"evidence": "extractor"})
            counts["edges"] += 1

    for revert in snapshot.get("reverts") or []:
        source = revert["commit"]
        target = revert["reverts"]
        upsert(store, graph, "Commit", f"commit:{source}", {"sha": source, "short_sha": source[:9], "subject": revert.get("subject") or "revert"})
        upsert(store, graph, "Commit", f"commit:{target}", {"sha": target, "short_sha": target[:9], "subject": ""})
        relate(store, graph, f"commit:{source}", "REVERTS", f"commit:{target}", {"evidence": revert.get("subject") or ""})
        counts["edges"] += 1

    _write_collaboration(store, graph)
    counts["writes"] = counts["nodes"]
    counts["nodes"] = store.ping(graph)
    from rationale.graphcache import invalidate

    invalidate(graph)
    logger.info("Loaded graph %s (%s)", graph, counts)
    return counts


def _upsert_person(store: Store, graph: str, person: dict[str, Any]) -> None:
    """Keep the newer activity timestamp. GitHub evidence is not replaced by a later snapshot that lacks it."""
    login = person["login"]
    node_id = f"person:{login}"
    incoming_last = int(person.get("last_active") or 0)
    incoming_evidence = person.get("activity_evidence") or "unknown"
    rows, _trace = store.query(
        graph,
        """
        MATCH (p:Person {id: $id})
        RETURN p.last_active AS last_active, p.activity_evidence AS activity_evidence
        """,
        {"id": node_id},
    )
    if rows:
        existing_last = int(rows[0].get("last_active") or 0)
        existing_evidence = rows[0].get("activity_evidence") or "unknown"
        incoming_last = max(existing_last, incoming_last)
        if incoming_evidence != "github" and existing_evidence == "github":
            incoming_evidence = "github"
    upsert(
        store,
        graph,
        "Person",
        node_id,
        {
            "login": login,
            "is_bot": bool(person.get("is_bot")),
            "last_active": incoming_last,
            "activity_evidence": incoming_evidence,
        },
    )


def _upsert_decision(store: Store, graph: str, node_id: str, props: dict[str, Any]) -> None:
    """MERGE a decision without wiping an override or a higher wrong_count already stored on the node."""
    keys = [key for key in props if key not in {"status", "wrong_count"} and PROP.fullmatch(key) and _clean(props[key]) is not None]
    params: dict[str, Any] = {
        "id": node_id,
        "status": props.get("status") or "accepted",
        "wrong_count": int(props.get("wrong_count") or 0),
    }
    assignments = ["n.id = $id"]
    for key in keys:
        params[key] = _clean(props[key])
        assignments.append(f"n.{key} = ${key}")
    cypher = (
        "MERGE (n:Decision {id: $id}) SET "
        + ", ".join(assignments)
        + """
        WITH n
        OPTIONAL MATCH (c:Correction {kind: 'override'})-[:ABOUT]->(n)
        SET n.status = CASE WHEN c IS NOT NULL THEN 'superseded' ELSE $status END,
            n.wrong_count = CASE
                WHEN coalesce(n.wrong_count, 0) > $wrong_count THEN n.wrong_count
                ELSE $wrong_count
            END
        """
    )
    store.query(graph, cypher, params, create=True)


def _write_collaboration(store: Store, graph: str) -> None:
    """Collaboration edges are written at load time so guard and who_knows stay read-only."""
    store.query(
        graph,
        """
        MATCH (p1:Person)-[:EDITED]->(f:File)<-[:EDITED]-(p2:Person)
        WHERE p1.is_bot = false AND p2.is_bot = false AND p1.id < p2.id
        MERGE (p1)-[:COLLABORATED]->(p2)
        MERGE (p2)-[:COLLABORATED]->(p1)
        """,
    )


def _symbols_for_decision(snapshot: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    source = decision.get("extracted_from") or ""
    paths: list[str] = []
    prs = snapshot.get("prs") or []
    if source.startswith("pr:"):
        number = int(source.split(":", 1)[1])
        for pr in prs:
            if int(pr["number"]) == number:
                paths.extend(item["path"] for item in pr.get("files") or [])
    elif source.startswith("doc:"):
        doc_path = source.split(":", 1)[1]
        paths.append(doc_path)
        for pr in prs:
            touched = [item["path"] for item in pr.get("files") or []]
            if doc_path in touched:
                paths.extend(touched)
    elif source.startswith("issue:"):
        number = int(source.split(":", 1)[1])
        for pr in prs:
            linked = [int(ref["number"]) for ref in (pr.get("references") or []) + (pr.get("fixes") or [])]
            if number in linked:
                paths.extend(item["path"] for item in pr.get("files") or [])
    symbols: list[str] = []
    for path in paths:
        for symbol in path_symbols(path):
            if symbol not in symbols:
                symbols.append(symbol)
    return symbols[:12]


def _pr_number_from_source(source: str, decision: dict[str, Any]) -> int | None:
    if source.startswith("pr:"):
        return int(source.split(":", 1)[1])
    return None


def _merge_sha(snapshot: dict[str, Any], number: int) -> str | None:
    for pr in snapshot.get("prs") or []:
        if int(pr["number"]) == number:
            return pr.get("merge_sha")
    return None


def _id_for_number(number: int, pr_numbers: set[int], issue_numbers: set[int]) -> str:
    if number in issue_numbers and number not in pr_numbers:
        return f"issue:{number}"
    if number in pr_numbers and number not in issue_numbers:
        return f"pr:{number}"
    if number in pr_numbers:
        return f"pr:{number}"
    return f"issue:{number}"


def _ensure_stub(store: Store, graph: str, node_id: str, number: int) -> None:
    if node_id.startswith("pr:"):
        ensure_node(store, graph, "PR", node_id, {"number": number, "state": "OPEN", "title": ""})
    elif node_id.startswith("issue:"):
        ensure_node(store, graph, "Issue", node_id, {"number": number, "state": "", "title": ""})


def _link_ref(store: Store, graph: str, pr_number: int, ref: dict[str, Any], rel: str, pr_numbers: set[int], issue_numbers: set[int]) -> None:
    number = int(ref["number"])
    if rel == "FIXES":
        target = f"issue:{number}"
        ensure_node(store, graph, "Issue", target, {"number": number, "state": "", "title": ""})
    else:
        target = _id_for_number(number, pr_numbers, issue_numbers)
        _ensure_stub(store, graph, target, number)
    relate(
        store,
        graph,
        f"pr:{pr_number}",
        rel,
        target,
        {"kind": ref.get("kind") or rel.lower(), "evidence": (ref.get("evidence") or "")[:200], "via": ref.get("via") or ""},
    )


