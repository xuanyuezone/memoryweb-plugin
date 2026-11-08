#!/usr/bin/env python3
"""Build the reproducible demo database used by this repo's acceptance tests.

Creates a fresh SQLite database by driving a real memoryweb MCP server over
stdio (via mcp_min_client) with fixed, deterministic content: three nodes in
two domains plus one edge. The fixture is fully self-made — no production
data — so it can be published and rebuilt anywhere the memoryweb binary and
CPython 3.9+ exist.

Usage:
    python3 tests/make_demo_db.py --db tests/data/demo.db
    python3 tests/make_demo_db.py --db /tmp/demo.db --binary /opt/mcp/memoryweb/memoryweb

The target file (and its -wal/-sidecars) is removed first, so re-running
always yields an identical fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_min_client import McpStdioClient  # noqa: E402

DEMO_NODES = [
    {
        "domain": "demo/decisions",
        "label": "Decision: ship memoryweb as a portable Agent Plugins v1 package",
        "description": "The integration is pure configuration (plugin.json + mcp.json) "
                       "with no Python code, installed from its own git repository.",
        "why_matters": "Configuration-only packages need no code execution, pass install "
                       "scans trivially and stay maintainable against a fast-moving core.",
        "node_kind": "decision",
        "tags": "portable,plugins-v1",
    },
    {
        "domain": "demo/findings",
        "label": "Finding: native mcp_servers config wins on a name clash",
        "description": "When config.yaml and a portable package declare the same server "
                       "name, Hermes keeps the native entry and skips the package server.",
        "why_matters": "Migration order becomes safe: enabling the plugin on a host that "
                       "already runs the native block changes nothing until the block is removed.",
        "node_kind": "finding",
        "tags": "precedence,migration",
    },
    {
        "domain": "demo/lexicon",
        "label": "Term: environment passthrough",
        "description": "Declaring \"NAME\": \"${NAME}\" in mcp.json forwards the user's "
                       "variable to the stdio server; an unset variable drops the entry.",
        "why_matters": "Keeps machine-specific paths out of the repository while letting "
                       "each host pin its own database path and Ollama endpoint.",
        "node_kind": "reference",
        "tags": "env,mcp-json",
    },
]

DEMO_EDGE = {
    "relationship": "governed_by",
    "narrative": "The passthrough design follows from the portability decision.",
}


def parse_result(result: dict) -> dict:
    text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    return json.loads(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="target database path (overwritten)")
    parser.add_argument("--binary", default="memoryweb",
                        help="memoryweb executable (default: resolved via PATH)")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    ids = []
    with McpStdioClient(args.binary, env={"MEMORYWEB_DB": str(db_path)}) as client:
        for node in DEMO_NODES:
            payload = parse_result(client.call_tool("remember", node))
            ids.append(payload["node"]["id"])
            print(f"remembered: {node['label']} -> {payload['node']['id']}")
        edge = {"from_memory": ids[0], "to_memory": ids[2], **DEMO_EDGE}
        parse_result(client.call_tool("connect", edge))
        print(f"connected: {ids[0]} --{DEMO_EDGE['relationship']}--> {ids[2]}")

        domains = parse_result(client.call_tool("domains", {"action": "list"}))
        count = parse_result(client.call_tool("search", {
            "query": "portable", "domain": "demo/decisions", "limit": 5}))
        print("domains:", json.dumps(domains.get("domains"), ensure_ascii=False))
        hits = count if isinstance(count, list) else count.get("results", count)
        print("search('portable') hits:", len(hits) if hasattr(hits, "__len__") else hits)

    print(f"demo database ready: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
