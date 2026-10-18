"""MCP server for Rationale Guard, over stdio and over HTTP POST /mcp.

Tools: guard_diff, why, who_knows, check_premises, record_feedback.
Graph errors are returned as tool errors. They are not rewritten as empty findings.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from rationale import __version__
from rationale.db import GraphError, GraphNotFound, InvalidGraphName, Store, graph_name_for_repo
from rationale.explain import why as explain_why
from rationale.guard import run_guard
from rationale.judge import JudgeError
from rationale.memory import record_correction
from rationale.premises import check_premises

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "guard_diff",
        "description": "Before a change lands, report which past decision it contradicts, whether the premise still holds, which alternatives were rejected, and who to ask.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "diff": {"type": "string", "description": "Unified diff"},
                "pr_url": {"type": "string"},
                "judge": {"type": "string", "enum": ["auto", "offline", "llm"]},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "why",
        "description": "Explain one decision from the graph: source chain, premise checks, rejected alternatives, and who to ask.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "decision_id": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "who_knows",
        "description": "Rank humans who edit a file, using FalkorDB PageRank or betweenness on the person-file graph.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "path": {"type": "string"}},
            "required": ["repo", "path"],
        },
    },
    {
        "name": "check_premises",
        "description": "Run premise-staleness Cypher (open linked issues, superseding PRs, reverted code, inactive authors) for one decision.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "decision_id": {"type": "string"}},
            "required": ["repo", "decision_id"],
        },
    },
    {
        "name": "record_feedback",
        "description": "Remember a correction. kind is ack, override, or wrong. override supersedes the decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "decision_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["ack", "override", "wrong"]},
                "text": {"type": "string"},
                "by": {"type": "string"},
            },
            "required": ["repo", "decision_id", "kind", "text"],
        },
    },
]


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "rationale-guard", "version": __version__},
            },
        )
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            payload = call_tool(params.get("name"), params.get("arguments") or {})
            return _result(msg_id, {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False})
        except GraphNotFound:
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": "graph not found"}], "isError": True},
            )
        except InvalidGraphName as exc:
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        except GraphError as exc:
            logger.error("Graph unavailable: %s", exc)
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": "graph unavailable"}], "isError": True},
            )
        except (JudgeError, ValueError) as exc:
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}


def call_tool(name: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
    repo = arguments.get("repo") or "paperclipai/paperclip"
    graph = graph_name_for_repo(repo)
    store = Store()
    if name == "guard_diff":
        diff = arguments.get("diff") or ""
        source_pr = None
        if arguments.get("pr_url"):
            from rationale.ingest.pipeline import parse_pr_url

            _pr_repo, source_pr = parse_pr_url(arguments["pr_url"])
            if not diff.strip():
                from rationale.api.main import _diff_for_pr

                diff = _diff_for_pr(arguments["pr_url"])
                repo = _pr_repo
                graph = graph_name_for_repo(repo)
        if not diff.strip():
            raise ValueError("guard_diff requires diff or pr_url")
        return run_guard(store, repo, diff, graph=graph, judge=arguments.get("judge"), source_pr=source_pr)
    if name == "why":
        decision_id = arguments.get("decision_id")
        if not decision_id and arguments.get("question"):
            from rationale.api.main import _find_decision

            decision_id = _find_decision(store, graph, arguments["question"])
        if not decision_id:
            raise ValueError("why requires decision_id or a question that matches a decision")
        return explain_why(store, graph, decision_id)
    if name == "who_knows":
        from rationale.experts import experts_for_paths

        people, traces = experts_for_paths(store, graph, [arguments["path"]])
        return {"ask": people, "cypher_trace": traces}
    if name == "check_premises":
        summary, traces = check_premises(store, graph, [arguments["decision_id"]])
        return {"premise": summary.get(arguments["decision_id"]), "cypher_trace": traces}
    if name == "record_feedback":
        return record_correction(
            store,
            graph,
            arguments["decision_id"],
            arguments["kind"],
            arguments["text"],
            arguments.get("by") or "agent",
        )
    raise ValueError(f"unknown tool {name}")


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def mcp_http(message: dict[str, Any]) -> dict[str, Any]:
    response = handle(message)
    return response or {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}


def serve_stdio() -> None:
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return
        text = line.decode().strip()
        if not text:
            continue
        if text.lower().startswith("content-length"):
            headers = [text]
            while True:
                nxt = stdin.readline().decode()
                if nxt in ("\r\n", "\n", ""):
                    break
                headers.append(nxt.strip())
            length = 0
            for header in headers:
                if header.lower().startswith("content-length"):
                    length = int(header.split(":", 1)[1].strip())
            message = json.loads(stdin.read(length).decode())
            response = handle(message)
            if response is not None:
                _write_frame(response)
            continue
        if text.startswith("{"):
            response = handle(json.loads(text))
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()


def _write_frame(payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def main() -> None:
    serve_stdio()


if __name__ == "__main__":
    main()
