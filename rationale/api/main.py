"""FastAPI surface. Database and judge failures are HTTP errors and are never cached."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rationale.db import GraphError, GraphNotFound, InvalidGraphName, Store, graph_name_for_repo, repo_from_graph, reset_db
from rationale.explain import why
from rationale.graphcache import run_parallel
from rationale.guard import run_guard
from rationale.ingest.github_api import GitHubError, fetch_pr_diff
from rationale.ingest.load import load_json, load_snapshot
from rationale.ingest.pipeline import parse_pr_url, snapshot_from_pr
from rationale.judge import JudgeError
from rationale.memory import list_corrections, record_correction
from rationale.mcp_server import mcp_http
from rationale.settings import repo_root

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_seed_status = {"state": "loading"}
_DB_LOCATION = re.compile(
    r"(?:rediss?|falkors?)://\S+"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b"
    r"|\b(?:localhost|[\w.-]+\.[\w.-]+):\d{2,5}\b",
    re.IGNORECASE,
)


def graph_client_message(exc: BaseException) -> str:
    """Log the driver error. Callers only ever show a fixed sentence."""
    logger.error("Graph unavailable: %s", exc)
    return "graph unavailable"


def redact_client_text(text: str) -> str:
    raw = text or ""
    lowered = raw.lower()
    if _DB_LOCATION.search(raw) or "falkordb" in lowered:
        return "graph unavailable"
    if "redis" in lowered and re.search(r":\d{2,5}", raw):
        return "graph unavailable"
    return raw


def visible_repos(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Graphs with no decisions are not choices on the guard screen."""
    return [item for item in repos if int(item.get("decisions") or 0) > 0]

_cache: dict[str, dict[str, Any]] = {}
_jobs: dict[str, dict[str, Any]] = {}
_ingest_hits: dict[str, list[float]] = {}
_lock = threading.Lock()


class GuardBody(BaseModel):
    repo: str = "paperclipai/paperclip"
    diff: str | None = None
    pr_url: str | None = None
    graph: str | None = None
    judge: str | None = None
    # None with a PR URL builds or updates that repo's graph, then guards the diff.
    ingest: bool | None = None


class FeedbackBody(BaseModel):
    repo: str = "paperclipai/paperclip"
    graph: str | None = None
    decision_id: str
    kind: str
    text: str
    by: str = "you"
    pr: int | None = None


class WhyBody(BaseModel):
    repo: str = "paperclipai/paperclip"
    graph: str | None = None
    decision_id: str | None = None
    question: str | None = None


class IngestBody(BaseModel):
    pr_url: str
    judge: str | None = None


class WhoBody(BaseModel):
    repo: str = "paperclipai/paperclip"
    graph: str | None = None
    path: str


def _store() -> Store:
    return Store()


def _graph(repo: str, graph: str | None) -> str:
    return graph or graph_name_for_repo(repo)


def seed_path() -> Path:
    return repo_root() / "data" / "seed" / "paperclip.json"


def ensure_seed(store: Store | None = None) -> None:
    path = seed_path()
    if not path.exists():
        logger.warning("No seed snapshot at %s", path)
        return
    snapshot = load_json(path)
    graph = graph_name_for_repo(snapshot["repo"])
    store = store or _store()
    try:
        count = store.ping(graph)
    except GraphNotFound:
        count = 0
    if count > 0:
        return
    logger.info("Loading seed into %s", graph)
    load_snapshot(store, snapshot, graph, reset=False)


def _load_seed_background() -> None:
    try:
        ensure_seed()
        _seed_status["state"] = "ready"
    except Exception as exc:
        logger.error("Startup seed load failed: %s", exc)
        _seed_status["state"] = "error"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_load_seed_background, name="seed-load", daemon=True).start()
    yield


app = FastAPI(title="Rationale Guard", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _decision_counts(store: Store, names: list[str]) -> list[dict[str, Any]]:
    def count(name: str) -> dict[str, Any]:
        try:
            rows, _trace = store.query(
                name,
                "MATCH (d:Decision) WHERE coalesce(d.extractor, '') <> 'correction' RETURN count(d) AS n",
            )
            decisions = int(rows[0]["n"]) if rows else 0
        except GraphError as exc:
            logger.error("Decision count failed for %s: %s", name, exc)
            decisions = 0
        return {"graph": name, "repo": repo_from_graph(name), "decisions": decisions}

    if not names:
        return []
    counted = run_parallel([(name, lambda name=name: count(name)) for name in names])
    return [counted[name] for name in names]


def _loading_health(target: str) -> dict[str, Any]:
    return {
        "ok": True,
        "falkordb": True,
        "loading": True,
        "graph": target,
        "nodes": 0,
        "edges": 0,
        "graphs": [],
        "repos": [],
    }


@app.exception_handler(InvalidGraphName)
async def invalid_graph_name(_request: Request, exc: InvalidGraphName) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(GraphNotFound)
async def missing_graph(_request: Request, exc: GraphNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "graph not found"})


@app.get("/api/health")
def health(graph: str | None = None) -> dict[str, Any]:
    loading = _seed_status["state"] == "loading"
    seed_name = graph_name_for_repo("paperclipai/paperclip")
    target = graph or seed_name
    try:
        store = _store()
        try:
            store.require(target)
        except GraphNotFound:
            if loading and graph is None:
                return _loading_health(target)
            raise

        def nodes() -> int:
            return store.ping(target)

        def edges() -> int:
            edge_rows, _trace = store.query(target, "MATCH ()-[r]->() RETURN count(r) AS edges")
            return int(edge_rows[0]["edges"]) if edge_rows else 0

        def repos() -> list[dict[str, Any]]:
            names = [name for name in store.list_graphs() if name.startswith("rationale__")]
            return visible_repos(_decision_counts(store, names))

        parts = run_parallel([("nodes", nodes), ("edges", edges), ("repos", repos)])
        return {
            "ok": True,
            "falkordb": True,
            "loading": loading,
            "graph": target,
            "nodes": parts["nodes"],
            "edges": parts["edges"],
            "graphs": [item["graph"] for item in parts["repos"]],
            "repos": parts["repos"],
        }
    except (InvalidGraphName, GraphNotFound):
        raise
    except Exception as exc:
        logger.error("Health check failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"ok": False, "falkordb": False, "loading": loading, "error": "graph unavailable"},
        ) from exc


@app.get("/api/graphs")
def graphs() -> dict[str, Any]:
    try:
        names = [name for name in _store().list_graphs() if name.startswith("rationale__")]
    except GraphError as exc:
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    return {"graphs": names}


@app.get("/api/samples")
def samples() -> dict[str, Any]:
    path = repo_root() / "eval" / "cases.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else []
    return {"samples": data or []}


@app.post("/api/guard")
def guard(body: GuardBody, request: Request) -> dict[str, Any]:
    diff = body.diff or ""
    repo = body.repo
    built_graph: str | None = None
    source_pr: int | None = None
    if body.pr_url:
        try:
            parsed_repo, source_pr = parse_pr_url(body.pr_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not diff.strip():
            repo = parsed_repo
        if not diff.strip() and body.ingest is not False:
            try:
                snapshot, built_graph, diff = _ingest_pr(body.pr_url, request)
            except GraphError as exc:
                logger.error("Ingest graph error: %s", exc)
                raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
            repo = snapshot["repo"]
        elif not diff.strip():
            diff = _diff_for_pr(body.pr_url)
    if not diff.strip():
        raise HTTPException(status_code=400, detail="Provide a unified diff or a public PR URL.")
    graph = built_graph or _graph(repo, body.graph)
    try:
        store = _store()
        store.require(graph)
        node_count = store.ping(graph)
    except (InvalidGraphName, GraphNotFound):
        raise
    except GraphError as exc:
        logger.error("Guard refused because FalkorDB is unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    key = hashlib.sha256(f"{graph}|{body.judge}|{source_pr}|{node_count}|{diff}".encode()).hexdigest()
    cached = _cache.get(key)
    if cached and cached.get("node_count") == node_count and not cached["value"].get("error"):
        payload = dict(cached["value"])
        payload["cached"] = False  # successful results may be reused; the flag stays false so a cached miss is never implied
        payload["from_cache"] = True
        return payload
    try:
        report = run_guard(store, repo, diff, graph=graph, judge=body.judge, source_pr=source_pr)
    except JudgeError as exc:
        logger.error("Guard judge error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except GraphError as exc:
        logger.error("Guard graph error: %s", exc)
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report["from_cache"] = False
    report["cached"] = False
    _cache[key] = {"node_count": node_count, "value": report}
    return report


@app.post("/api/why")
def ask_why(body: WhyBody) -> dict[str, Any]:
    graph = _graph(body.repo, body.graph)
    try:
        store = _store()
        decision_id = body.decision_id or _find_decision(store, graph, body.question or "")
        if not decision_id:
            raise HTTPException(status_code=404, detail="No decision in the graph matches that question.")
        return why(store, graph, decision_id)
    except HTTPException:
        raise
    except GraphError as exc:
        logger.error("Why failed: %s", exc)
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/feedback")
def feedback(body: FeedbackBody) -> dict[str, Any]:
    graph = _graph(body.repo, body.graph)
    try:
        result = record_correction(_store(), graph, body.decision_id, body.kind, body.text, body.by, body.pr)
    except GraphError as exc:
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _cache.clear()
    return result


@app.get("/api/learned")
def learned(repo: str = "paperclipai/paperclip", graph: str | None = None) -> dict[str, Any]:
    target = _graph(repo, graph)
    try:
        rows, trace = list_corrections(_store(), target)
    except GraphError as exc:
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    return {"learned": rows, "cypher_trace": [trace]}


@app.post("/api/who")
def who(body: WhoBody) -> dict[str, Any]:
    from rationale.experts import experts_for_paths

    graph = _graph(body.repo, body.graph)
    try:
        people, traces = experts_for_paths(_store(), graph, [body.path])
    except GraphError as exc:
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    return {"ask": people, "cypher_trace": traces}


@app.post("/api/ingest")
def ingest(body: IngestBody, request: Request) -> dict[str, Any]:
    try:
        parse_pr_url(body.pr_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _limit_ingest(request.client.host if request.client else "local")
    _prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "created": time.time(),
        "events": [{"step": "queued", "detail": body.pr_url}],
        "done": False,
        "error": None,
        "result": None,
    }
    _jobs[job_id] = job

    def run() -> None:
        try:
            def progress(event: dict[str, Any]) -> None:
                job["events"].append(event)

            snapshot, graph, diff = _load_pr_snapshot(body.pr_url, progress)
            _cache.clear()
            _repo_name, source_pr = parse_pr_url(body.pr_url)
            report = run_guard(_store(), snapshot["repo"], diff, graph=graph, judge=body.judge, source_pr=source_pr)
            job["result"] = {
                "repo": snapshot["repo"],
                "graph": graph,
                "decisions": len(snapshot.get("decisions") or []),
                "guard": report,
            }
        except Exception as exc:
            logger.error("Ingest failed: %s", exc)
            job["error"] = redact_client_text(str(exc))
        finally:
            job["done"] = True
            job["events"].append({"step": "done", "error": job["error"]})

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/ingest/{job_id}")
def ingest_status(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown ingest job")
    return job


@app.get("/api/ingest/{job_id}/events")
def ingest_events(job_id: str) -> StreamingResponse:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown ingest job")

    def stream():
        index = 0
        while True:
            while index < len(job["events"]):
                yield f"data: {json.dumps(job['events'][index])}\n\n"
                index += 1
            if job["done"]:
                yield f"data: {json.dumps({'done': True, 'error': job.get('error')})}\n\n"
                return
            time.sleep(0.4)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/mcp")
async def mcp(request: Request) -> dict[str, Any]:
    payload = await request.json()
    try:
        return mcp_http(payload)
    except GraphError as exc:
        return {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32000, "message": graph_client_message(exc)}}


def _limit_ingest(ip: str) -> None:
    now = time.time()
    with _lock:
        hits = [stamp for stamp in _ingest_hits.get(ip, []) if now - stamp < 3600]
        if len(hits) >= 2:
            raise HTTPException(status_code=429, detail="Ingest rate limit is 2 per hour for this IP.")
        hits.append(now)
        _ingest_hits[ip] = hits


def _prune_jobs() -> None:
    if len(_jobs) <= 40:
        return
    finished = sorted(
        ((job.get("created") or 0, job_id) for job_id, job in _jobs.items() if job.get("done")),
        key=lambda item: item[0],
    )
    for _created, job_id in finished[: max(0, len(_jobs) - 40)]:
        _jobs.pop(job_id, None)


def _load_pr_snapshot(pr_url: str, progress: Any = None) -> tuple[dict[str, Any], str, str]:
    """Merge a public PR into its repo graph. Corrections and superseded decisions stay."""
    snapshot = snapshot_from_pr(pr_url, mode="auto", progress=progress)
    graph = graph_name_for_repo(snapshot["repo"])
    load_snapshot(_store(), snapshot, graph, reset=False)
    _repo, number = parse_pr_url(pr_url)
    diff = fetch_pr_diff(snapshot["repo"], number)
    return snapshot, graph, diff


def _ingest_pr(pr_url: str, request: Request) -> tuple[dict[str, Any], str, str]:
    repo, number = parse_pr_url(pr_url)
    graph = graph_name_for_repo(repo)
    try:
        already = _pr_loaded(_store(), graph, number)
    except GraphError as exc:
        raise HTTPException(status_code=503, detail=graph_client_message(exc)) from exc
    if not already:
        _limit_ingest(request.client.host if request.client else "local")
    try:
        return _load_pr_snapshot(pr_url)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _pr_loaded(store: Store, graph: str, number: int) -> bool:
    try:
        if store.ping(graph) == 0:
            return False
    except (GraphError, GraphNotFound, InvalidGraphName):
        return False
    rows, _trace = store.query(
        graph,
        "MATCH (p:PR {number: $number}) RETURN p.id AS id LIMIT 1",
        {"number": int(number)},
    )
    return bool(rows)


def _diff_for_pr(pr_url: str) -> str:
    repo, number = parse_pr_url(pr_url)
    try:
        return fetch_pr_diff(repo, number)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _find_decision(store: Store, graph: str, question: str) -> str | None:
    if not question.strip():
        return None
    query = " ".join(token for token in question.split() if len(token) > 2)[:200]
    try:
        rows, _trace = store.query(
            graph,
            """
            CALL db.idx.fulltext.queryNodes('Decision', $q) YIELD node, score
            RETURN node.id AS id, score
            ORDER BY score DESC
            LIMIT 1
            """,
            {"q": query},
        )
        if rows:
            return rows[0]["id"]
    except GraphError as exc:
        logger.info("Fulltext lookup failed, using CONTAINS: %s", exc)
    rows, _trace = store.query(
        graph,
        """
        MATCH (d:Decision)
        WHERE toLower(d.summary) CONTAINS toLower($q) OR toLower(d.source_quote) CONTAINS toLower($q)
        RETURN d.id AS id
        LIMIT 1
        """,
        {"q": question.lower()[:80]},
    )
    return rows[0]["id"] if rows else None


def mount_static() -> None:
    dist = repo_root() / "web" / "dist"
    if not dist.exists():
        return
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


mount_static()


def reset_state() -> None:
    _cache.clear()
    _jobs.clear()
    _ingest_hits.clear()
    reset_db()
