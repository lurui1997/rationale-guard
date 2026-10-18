"""In-memory copies of graph data that does not change between ingests.

PageRank, betweenness, and the decision-to-file map are the same for every
diff until a snapshot load or a correction. Keeping them here turns a WAN
round trip into a dict lookup. Call ``invalidate`` after any write.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

_lock = threading.Lock()
_slots: dict[tuple[str, str], Any] = {}


def invalidate(graph: str | None = None) -> None:
    with _lock:
        if graph is None:
            _slots.clear()
            return
        for key in [item for item in _slots if item[0] == graph]:
            _slots.pop(key, None)


def get_cached(graph: str, slot: str, loader: Callable[[], Any]) -> Any:
    """Return a stored loader result. Hits copy traces so ``ms`` can read 0."""
    key = (graph, slot)
    with _lock:
        hit = _slots.get(key)
    if hit is not None:
        return _mark_cached(_copy(hit))
    loaded = loader()
    with _lock:
        _slots.setdefault(key, _copy(loaded))
    return loaded


def run_parallel(jobs: list[tuple[str, Callable[[], Any]]]) -> dict[str, Any]:
    """Run independent FalkorDB calls together. One job stays on this thread."""
    if not jobs:
        return {}
    if len(jobs) == 1:
        name, fn = jobs[0]
        return {name: fn()}
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs}
        return {name: future.result() for name, future in futures.items()}


def _copy(value: Any) -> Any:
    if isinstance(value, tuple):
        return tuple(_copy(item) for item in value)
    if isinstance(value, list):
        return [_copy(item) for item in value]
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    return value


def _mark_cached(value: Any) -> Any:
    if isinstance(value, tuple):
        return tuple(_mark_cached(item) for item in value)
    if isinstance(value, list):
        return [_mark_cached(item) for item in value]
    if isinstance(value, dict) and "cypher" in value and "ms" in value:
        value["ms"] = 0
        value["cached"] = True
    return value
