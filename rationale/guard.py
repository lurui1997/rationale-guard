"""Decision guard.

The graph picks candidate decisions from the files and symbols in the diff.
Premise checks, rejected alternatives, and experts are Cypher. The judge only
labels candidates the graph already returned.
"""

from __future__ import annotations

import time
from typing import Any

from rationale.db import Store, graph_name_for_repo
from rationale.diffutil import ParsedDiff, parse_diff
from rationale.experts import experts_for_paths
from rationale.ingest.extract import is_scope_note
from rationale.judge import JudgeError, _content_overlap, judge_candidates, resolve_judge_mode
from rationale.premises import check_premises

CANDIDATE_QUERIES = [
    (
        "decision_via_pr_file",
        """
        MATCH (f:File) WHERE f.path IN $paths
        MATCH (pr:PR)-[:TOUCHES]->(f)
        MATCH (d:Decision)-[:DERIVED_FROM]->(pr)
        WHERE coalesce(d.status, 'accepted') <> 'superseded'
          AND coalesce(d.extractor, '') <> 'correction'
        RETURN d.id AS decision_id, d.summary AS summary, d.source_quote AS quote,
               d.source_url AS source_url, d.status AS status, d.confidence AS confidence,
               d.rationale AS rationale, d.wrong_count AS wrong_count, d.symbols AS symbols,
               d.extracted_from AS extracted_from,
               f.path AS path, pr.number AS via_number, pr.id AS via_id, 'pr-file' AS hop
        """,
    ),
    (
        "decision_via_issue",
        """
        MATCH (f:File) WHERE f.path IN $paths
        MATCH (pr:PR)-[:TOUCHES]->(f)
        MATCH (pr)-[:FIXES]->(i:Issue)<-[:DERIVED_FROM]-(d:Decision)
        WHERE coalesce(d.status, 'accepted') <> 'superseded'
          AND coalesce(d.extractor, '') <> 'correction'
        RETURN d.id AS decision_id, d.summary AS summary, d.source_quote AS quote,
               d.source_url AS source_url, d.status AS status, d.confidence AS confidence,
               d.rationale AS rationale, d.wrong_count AS wrong_count, d.symbols AS symbols,
               d.extracted_from AS extracted_from,
               f.path AS path, i.number AS via_number, i.id AS via_id, 'issue' AS hop
        """,
    ),
    (
        "decision_via_reference",
        """
        MATCH (f:File) WHERE f.path IN $paths
        MATCH (pr:PR)-[:TOUCHES]->(f)
        MATCH (pr)-[:REFERENCES]->(i:Issue)<-[:DERIVED_FROM]-(d:Decision)
        WHERE coalesce(d.status, 'accepted') <> 'superseded'
          AND coalesce(d.extractor, '') <> 'correction'
        RETURN d.id AS decision_id, d.summary AS summary, d.source_quote AS quote,
               d.source_url AS source_url, d.status AS status, d.confidence AS confidence,
               d.rationale AS rationale, d.wrong_count AS wrong_count, d.symbols AS symbols,
               d.extracted_from AS extracted_from,
               f.path AS path, i.number AS via_number, i.id AS via_id, 'reference' AS hop
        """,
    ),
    (
        "decision_via_doc",
        """
        MATCH (f:File) WHERE f.path IN $paths
        MATCH (pr:PR)-[:TOUCHES]->(f)
        MATCH (pr)-[:ADDS]->(doc:Doc)<-[:DERIVED_FROM]-(d:Decision)
        WHERE coalesce(d.status, 'accepted') <> 'superseded'
          AND coalesce(d.extractor, '') <> 'correction'
        RETURN d.id AS decision_id, d.summary AS summary, d.source_quote AS quote,
               d.source_url AS source_url, d.status AS status, d.confidence AS confidence,
               d.rationale AS rationale, d.wrong_count AS wrong_count, d.symbols AS symbols,
               d.extracted_from AS extracted_from,
               f.path AS path, pr.number AS via_number, doc.id AS via_id, 'doc' AS hop
        """,
    ),
    (
        "decision_on_same_path",
        """
        MATCH (f:File) WHERE f.path IN $paths
        MATCH (d:Decision)-[:DERIVED_FROM]->(doc:Doc)
        WHERE doc.path = f.path AND coalesce(d.status, 'accepted') <> 'superseded'
          AND coalesce(d.extractor, '') <> 'correction'
        RETURN d.id AS decision_id, d.summary AS summary, d.source_quote AS quote,
               d.source_url AS source_url, d.status AS status, d.confidence AS confidence,
               d.rationale AS rationale, d.wrong_count AS wrong_count, d.symbols AS symbols,
               d.extracted_from AS extracted_from,
               f.path AS path, 0 AS via_number, doc.id AS via_id, 'doc-path' AS hop
        """,
    ),
]

SYMBOL_QUERY = (
    "decision_via_symbol",
    """
    MATCH (d:Decision)
    WHERE coalesce(d.status, 'accepted') <> 'superseded'
      AND coalesce(d.extractor, '') <> 'correction'
      AND any(sym IN d.symbols WHERE sym IN $symbols)
    RETURN d.id AS decision_id, d.summary AS summary, d.source_quote AS quote,
           d.source_url AS source_url, d.status AS status, d.confidence AS confidence,
           d.rationale AS rationale, d.wrong_count AS wrong_count, d.symbols AS symbols,
           d.extracted_from AS extracted_from,
           '' AS path, 0 AS via_number, '' AS via_id, 'symbol' AS hop
    """,
)

LEARNED_QUERY = """
MATCH (f:File) WHERE f.path IN $paths
MATCH (pr:PR)-[:TOUCHES]->(f)
MATCH (d:Decision {status: 'superseded'})-[:DERIVED_FROM]->(pr)
MATCH (c:Correction)-[:ABOUT]->(d)
OPTIONAL MATCH (n:Decision)-[:SUPERSEDES]->(d)
RETURN DISTINCT d.id AS decision_id, d.summary AS summary, c.kind AS kind, c.by AS by,
       c.text AS text, c.at AS at, n.id AS successor_id, n.summary AS successor_summary
"""

ALT_QUERY = """
MATCH (alt:Alternative)-[:REJECTED_FOR]->(d:Decision)
WHERE d.id IN $ids
OPTIONAL MATCH (alt)-[:DESCRIBED_IN]->(node)
RETURN d.id AS decision_id, alt.description AS description, alt.reason_rejected AS reason,
       alt.ref AS ref, node.url AS url, node.number AS number, node.id AS node_id
"""

CORRECTION_QUERY = """
MATCH (c:Correction)-[:ABOUT]->(d:Decision)
WHERE d.id IN $ids
RETURN d.id AS decision_id, c.kind AS kind, c.by AS by, c.text AS text, c.at AS at
ORDER BY c.at DESC
"""


def rank_score(file_hits: int, symbol_hits: int, confidence: float, wrong_count: int) -> float:
    base = file_hits * 3 + symbol_hits + float(confidence or 0)
    return base * (0.25 ** int(wrong_count or 0))


def select_top(
    candidates: list[dict[str, Any]],
    limit: int = 8,
    minimum: float = 0.5,
    per_source: int = 2,
) -> list[dict[str, Any]]:
    """Keep about eight candidates. Equal scores break on hunk overlap, and one PR cannot fill every slot."""
    ranked = [item for item in candidates if item["score"] >= minimum]
    ranked.sort(key=lambda item: (item["score"], float(item.get("overlap") or 0)), reverse=True)
    chosen: list[dict[str, Any]] = []
    used: set[str] = set()
    counts: dict[str, int] = {}
    for item in ranked:
        source = item.get("extracted_from") or item["id"]
        if counts.get(source, 0) >= per_source:
            continue
        counts[source] = counts.get(source, 0) + 1
        chosen.append(item)
        used.add(item["id"])
        if len(chosen) >= limit:
            return chosen
    for item in ranked:
        if item["id"] in used:
            continue
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def _hunk_text(parsed: ParsedDiff) -> str:
    chunks = []
    for item in parsed.files:
        chunks.append(item.path)
        chunks.append(item.added_text())
        chunks.append(item.removed_text())
    return "\n".join(chunks)


def run_guard(
    store: Store,
    repo: str,
    diff: str,
    graph: str | None = None,
    judge: str | None = None,
    source_pr: int | None = None,
) -> dict[str, Any]:
    mode = resolve_judge_mode(judge)
    graph_name = graph or graph_name_for_repo(repo)
    parsed = parse_diff(diff)
    traces: list[dict[str, Any]] = []
    started = time.perf_counter()
    grouped: dict[str, dict[str, Any]] = {}
    params = {"paths": parsed.paths or ["__none__"], "symbols": parsed.symbols() or ["__none__"]}
    queries = list(CANDIDATE_QUERIES)
    if parsed.symbols():
        queries.append(SYMBOL_QUERY)
    for name, cypher in queries:
        rows, trace = store.query(graph_name, cypher, params)
        trace["name"] = name
        traces.append(trace)
        for row in rows:
            decision_id = row.get("decision_id")
            if not decision_id:
                continue
            quote = row.get("quote") or ""
            summary = row.get("summary") or ""
            if (row.get("status") or "") == "scope" or is_scope_note(quote) or is_scope_note(summary):
                continue
            if source_pr and (row.get("extracted_from") or "") == f"pr:{int(source_pr)}":
                continue
            bucket = grouped.setdefault(
                decision_id,
                {
                    "id": decision_id,
                    "summary": row.get("summary") or "",
                    "source_quote": row.get("quote") or "",
                    "source_url": row.get("source_url") or "",
                    "status": row.get("status") or "accepted",
                    "confidence": row.get("confidence") or 0,
                    "rationale": row.get("rationale") or "",
                    "wrong_count": int(row.get("wrong_count") or 0),
                    "symbols": row.get("symbols") or [],
                    "extracted_from": row.get("extracted_from") or "",
                    "paths": [],
                    "hops": [],
                    "vias": [],
                    "queries": [],
                },
            )
            if name not in bucket["queries"]:
                bucket["queries"].append(name)
            path = row.get("path") or ""
            if path and path not in bucket["paths"]:
                bucket["paths"].append(path)
            hop = row.get("hop")
            if hop and hop not in bucket["hops"]:
                bucket["hops"].append(hop)
            if row.get("via_id"):
                bucket["vias"].append({"id": row.get("via_id"), "number": row.get("via_number"), "hop": hop})

    symbol_set = set(parsed.symbols())
    hunks = _hunk_text(parsed)
    prepared = []
    for bucket in grouped.values():
        symbol_hits = len(set(bucket["symbols"] or []) & symbol_set)
        file_hits = len(bucket["paths"])
        if "symbol" in bucket["hops"] and file_hits == 0:
            symbol_hits = max(symbol_hits, 1)
        score = rank_score(file_hits, symbol_hits, bucket["confidence"], bucket["wrong_count"])
        overlap = _content_overlap(hunks, f"{bucket['summary']}\n{bucket['source_quote']}")
        bucket["file_hits"] = file_hits
        bucket["symbol_hits"] = symbol_hits
        bucket["overlap"] = round(overlap, 4)
        bucket["score"] = round(score, 4)
        prepared.append(bucket)
    chosen = select_top(prepared)
    ids = [item["id"] for item in chosen]
    premises, premise_traces = check_premises(store, graph_name, ids)
    traces.extend(premise_traces)
    alternatives = _alternatives(store, graph_name, ids, traces)
    corrections = _corrections(store, graph_name, ids, traces)
    learned_rows, learned_trace = store.query(graph_name, LEARNED_QUERY, {"paths": params["paths"]})
    learned_trace["name"] = "learned_overrides"
    traces.append(learned_trace)
    diff_paths = list(parsed.paths)
    touched = list(diff_paths)
    for item in chosen:
        for path in item["paths"]:
            if path and path not in touched:
                touched.append(path)
    ask, expert_traces = experts_for_paths(store, graph_name, touched[:30])
    traces.extend(expert_traces)
    graph_ms = round((time.perf_counter() - started) * 1000, 2)
    trace_by_name = _dedupe_traces(traces)

    for item in chosen:
        item["alternatives"] = alternatives.get(item["id"], [])
    verdicts = judge_candidates(chosen, parsed, mode)
    findings = []
    unrelated = 0
    for item, verdict in zip(chosen, verdicts):
        if verdict["verdict"] == "unrelated":
            unrelated += 1
            continue
        learned = _latest(corrections.get(item["id"], []))
        premise = premises.get(item["id"], {"status": "holds", "rules": []})
        findings.append(
            {
                "decision_id": item["id"],
                "verdict": verdict["verdict"],
                "judge": verdict["judge"],
                "reason": verdict["reason"],
                "hunk_ref": verdict.get("hunk_ref") or "",
                "summary": item["summary"],
                "quote": item["source_quote"],
                "source_url": item["source_url"],
                "status": item["status"],
                "score": item["score"],
                "hops": item["hops"],
                "premise": premise,
                "rejected_alternatives": item["alternatives"],
                "ask": ask,
                "learned_from": learned,
                "subgraph": _subgraph(item, premise, item["alternatives"], learned, ask),
                "cypher_trace": _finding_trace(item, premise, trace_by_name),
            }
        )
    contradicting = [
        finding
        for finding in findings
        if finding["verdict"] == "contradicts" and (not finding["learned_from"] or finding["learned_from"]["kind"] != "ack")
    ]
    status = "warn" if contradicting else "pass"
    summary = _summary(status, findings, learned_rows, judged=len(chosen), unrelated=unrelated)
    return {
        "repo": repo,
        "graph": graph_name,
        "status": status,
        "summary": summary,
        "judge": mode,
        "graph_ms": graph_ms,
        "paths": diff_paths,
        "findings": findings,
        "learned": learned_rows,
        "cypher_trace": list(trace_by_name.values()),
    }


def _alternatives(store: Store, graph: str, ids: list[str], traces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not ids:
        return {}
    rows, trace = store.query(graph, ALT_QUERY, {"ids": ids})
    trace["name"] = "rejected_alternatives"
    traces.append(trace)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["decision_id"], []).append(
            {
                "description": row.get("description") or "",
                "reason": row.get("reason") or "",
                "ref": row.get("ref") or "",
                "url": row.get("url") or "",
                "number": row.get("number"),
            }
        )
    return grouped


def _corrections(store: Store, graph: str, ids: list[str], traces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not ids:
        return {}
    rows, trace = store.query(graph, CORRECTION_QUERY, {"ids": ids})
    trace["name"] = "corrections"
    traces.append(trace)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["decision_id"], []).append(
            {"kind": row.get("kind"), "by": row.get("by"), "text": row.get("text"), "at": row.get("at")}
        )
    return grouped


def _latest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return items[0]


def _subgraph(item: dict[str, Any], premise: dict[str, Any] | None, alternatives: list[dict[str, Any]], learned: dict[str, Any] | None, ask: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = [{"id": item["id"], "label": "Decision", "title": item["summary"][:120]}]
    edges = []
    if item.get("extracted_from"):
        source = item["extracted_from"]
        label = source.split(":", 1)[0].upper() if ":" in source else "Source"
        label = {"PR": "PR", "ISSUE": "Issue", "DOC": "Doc"}.get(label, "Source")
        nodes.append({"id": source, "label": label, "title": source})
        edges.append({"source": item["id"], "target": source, "type": "DERIVED_FROM"})
    for path in item.get("paths") or []:
        file_id = f"file:{path}"
        nodes.append({"id": file_id, "label": "File", "title": path})
        if item.get("extracted_from"):
            edges.append({"source": item["extracted_from"], "target": file_id, "type": "TOUCHES"})
    for rule in (premise or {}).get("rules") or []:
        for evidence in rule.get("evidence") or []:
            nodes.append({"id": evidence, "label": "Evidence", "title": f"{rule['id']}: {evidence}"})
            edges.append({"source": item["id"], "target": evidence, "type": rule["id"]})
    for alt in alternatives:
        alt_id = f"alt:{alt.get('ref') or alt.get('description', '')[:24]}"
        nodes.append({"id": alt_id, "label": "Alternative", "title": (alt.get("description") or "")[:120]})
        edges.append({"source": alt_id, "target": item["id"], "type": "REJECTED_FOR"})
    if learned:
        nodes.append({"id": f"learned:{learned.get('kind')}", "label": "Correction", "title": learned.get("text") or ""})
        edges.append({"source": f"learned:{learned.get('kind')}", "target": item["id"], "type": "ABOUT"})
    for person in ask[:3]:
        nodes.append({"id": f"person:{person['login']}", "label": "Person", "title": person["login"]})
        edges.append({"source": f"person:{person['login']}", "target": item["id"], "type": "EXPERT"})
    dedup_nodes = []
    seen = set()
    for node in nodes:
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        dedup_nodes.append(node)
    dedup_edges = []
    seen_edges = set()
    for edge in edges:
        key = (edge["source"], edge["type"], edge["target"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        dedup_edges.append(edge)
    return {"nodes": dedup_nodes, "edges": dedup_edges}


def _dedupe_traces(traces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for trace in traces:
        name = trace.get("name") or "query"
        if name not in deduped:
            deduped[name] = trace
    return deduped


def _finding_trace(item: dict[str, Any], premise: dict[str, Any], trace_by_name: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Queries that selected this decision and the premise rules that hit it. Not the whole guard trace."""
    names = list(item.get("queries") or [])
    if item.get("alternatives"):
        names.append("rejected_alternatives")
    names.extend(rule.get("id") for rule in (premise.get("rules") or []) if rule.get("id"))
    names.append("corrections")
    seen: set[str] = set()
    own: list[dict[str, Any]] = []
    for name in names:
        if not name or name in seen or name not in trace_by_name:
            continue
        seen.add(name)
        own.append(trace_by_name[name])
    return own


def _summary(
    status: str,
    findings: list[dict[str, Any]],
    learned: list[dict[str, Any]],
    judged: int = 0,
    unrelated: int = 0,
) -> str:
    contradicts = [finding for finding in findings if finding["verdict"] == "contradicts"]
    if status == "warn":
        first = contradicts[0]
        return f"This change contradicts \"{first['summary'][:140]}\" ({first['decision_id']})."
    if learned:
        latest = learned[0]
        return (
            f"No contradiction. Learned from @{latest.get('by')}: {latest.get('kind')} "
            f"on {latest.get('decision_id')}."
        )
    if findings:
        return "No contradiction. Related decisions are listed and were not judged to be reversals."
    if judged and unrelated:
        return (
            f"The graph found {judged} past decision(s) on these files. "
            "The judge marked them unrelated to this diff."
        )
    return "No past decision in this graph touches the changed files."
