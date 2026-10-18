"""FalkorDB connection and parameterized query helper.

``FALKORDB_URL`` accepts ``redis://`` and ``rediss://`` (also ``falkor://`` and
``falkors://``). The scheme is passed through to the client unchanged, so TLS is
used only when the URL asks for it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from falkordb import FalkorDB

from rationale.settings import env

logger = logging.getLogger(__name__)

GRAPH_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
URL_SCHEMES = {"redis": False, "rediss": True, "falkor": False, "falkors": True, "unix": False}

_db: FalkorDB | None = None


class GraphError(RuntimeError):
    """Raised when FalkorDB cannot be reached or a query fails. Never swallow this."""


def describe_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in URL_SCHEMES:
        raise GraphError(
            f"Unsupported FALKORDB_URL scheme '{scheme}'. Use redis:// or rediss://."
        )
    return {
        "scheme": "rediss" if URL_SCHEMES[scheme] else scheme,
        "tls": URL_SCHEMES[scheme],
        "host": parsed.hostname,
        "port": parsed.port,
    }


def connect(url: str | None = None) -> FalkorDB:
    target = url if url is not None else env("FALKORDB_URL")
    try:
        if target:
            describe_url(target)
            logger.info("Connecting to FalkorDB via URL scheme %s", urlparse(target).scheme)
            return FalkorDB.from_url(target, socket_connect_timeout=5, socket_timeout=5)
        host = env("FALKORDB_HOST", "localhost") or "localhost"
        port = int(env("FALKORDB_PORT", "6379") or "6379")
        password = env("FALKORDB_PASSWORD")
        username = env("FALKORDB_USERNAME")
        logger.info("Connecting to FalkorDB at %s:%s (plaintext)", host, port)
        return FalkorDB(
            host=host,
            port=port,
            password=password,
            username=username,
            ssl=False,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    except GraphError:
        raise
    except Exception as exc:
        raise GraphError(f"FalkorDB connection failed: {exc}") from exc


def get_db() -> FalkorDB:
    global _db
    if _db is None:
        _db = connect()
    return _db


def reset_db() -> None:
    global _db
    _db = None


def repo_from_graph(name: str) -> str | None:
    """Inverse of ``graph_name_for_repo`` when owner and name contain no sanitized characters."""
    prefix = "rationale__"
    if not name.startswith(prefix):
        return None
    owner, sep, repo = name[len(prefix):].partition("__")
    if not sep or not owner or not repo:
        return None
    return f"{owner}/{repo}"


def graph_name_for_repo(repo: str) -> str:
    if not repo or "/" not in repo:
        raise ValueError("repo must look like owner/name")
    owner, name = repo.split("/", 1)
    raw = f"rationale__{owner}__{name}"
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    if not safe or safe[0].isdigit():
        safe = "g_" + safe
    safe = safe[:64]
    if not GRAPH_NAME.fullmatch(safe):
        raise ValueError(f"invalid graph name derived from {repo}")
    return safe


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    labels = getattr(value, "labels", None)
    properties = getattr(value, "properties", None)
    if labels is not None and properties is not None:
        props = dict(properties)
        props["_labels"] = list(labels)
        return props
    relation = getattr(value, "relation", None)
    if relation is not None and properties is not None:
        return {"type": relation, "properties": dict(properties)}
    if isinstance(value, (list, tuple)):
        return [_cell(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _cell(v) for k, v in value.items()}
    return str(value)


def _header_names(result: Any) -> list[str]:
    header = getattr(result, "header", None) or []
    names: list[str] = []
    for index, column in enumerate(header):
        if isinstance(column, (list, tuple)) and len(column) >= 2:
            names.append(str(column[1]))
        elif isinstance(column, str):
            names.append(column)
        else:
            name = getattr(column, "name", None) or getattr(column, "column_name", None)
            names.append(str(name) if name else f"col{index}")
    return names


def records(result: Any) -> list[dict[str, Any]]:
    rows = getattr(result, "result_set", None) or []
    names = _header_names(result)
    output: list[dict[str, Any]] = []
    for row in rows:
        if not names:
            names = [f"col{i}" for i in range(len(row))]
        output.append({names[i] if i < len(names) else f"col{i}": _cell(row[i]) for i in range(len(row))})
    return output


class Store:
    def __init__(self, db: FalkorDB | None = None):
        self.db = db or get_db()

    def graph(self, name: str):
        if not GRAPH_NAME.fullmatch(name):
            raise ValueError(f"refusing graph name {name!r}")
        return self.db.select_graph(name)

    def query(
        self,
        name: str,
        cypher: str,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started = time.perf_counter()
        try:
            result = self.graph(name).query(cypher, params or {}, timeout=timeout)
        except Exception as exc:  # driver raises ResponseError and connection errors
            raise GraphError(f"FalkorDB query failed: {exc}") from exc
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        trace = {
            "cypher": cypher.strip(),
            "params": _public_params(params or {}),
            "ms": elapsed,
        }
        return records(result), trace

    def delete_graph(self, name: str) -> None:
        try:
            self.graph(name).delete()
        except Exception as exc:
            message = str(exc).lower()
            if "invalid graph operation" in message or "no such graph" in message or "empty key" in message:
                return
            # FalkorDB raises when the key is absent. Surface anything else.
            if "does not exist" in message or "unknown graph" in message:
                return
            raise GraphError(f"FalkorDB delete failed: {exc}") from exc

    def list_graphs(self) -> list[str]:
        try:
            raw = self.db.list_graphs()
        except Exception as exc:
            raise GraphError(f"FalkorDB GRAPH.LIST failed: {exc}") from exc
        names = []
        for item in raw or []:
            names.append(item.decode() if isinstance(item, bytes) else str(item))
        return names

    def ping(self, name: str) -> int:
        rows, _trace = self.query(name, "MATCH (n) RETURN count(n) AS n")
        if not rows:
            return 0
        return int(rows[0].get("n") or 0)


def _public_params(params: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str) and len(value) > 500:
            cleaned[key] = value[:500] + "…"
        elif isinstance(value, list) and len(value) > 40:
            cleaned[key] = value[:40]
        else:
            cleaned[key] = value
    return cleaned
