#!/usr/bin/env python3
"""Snapshot (and optionally exercise) the MCP tool surface through Hermes's
real session path — plugin discovery, MCP config merge, server connect and
the standard tool dispatch.

Run it with the Hermes runtime's interpreter (the same python that has the
`hermes` package importable), e.g. inside the official container:

    docker exec <container> python3 /host-tests/hermes_surface_probe.py            # surface only
    docker exec <container> python3 /host-tests/hermes_surface_probe.py --calls    # + tool calls

Surface mode prints one JSON object: every MCP tool name the session would
register (empty list when no server is configured or every plugin is
disabled). Call mode additionally invokes three read-only memoryweb tools
(domains / search / recall) through model_tools.handle_function_call — the
same dispatch a live session uses — and prints their raw JSON replies, so
two runs (native config vs enabled plugin) can be diffed for parity.
"""

from __future__ import annotations

import json
import sys


def normalize(value, round_floats=3):
    """Make outputs comparable across runs: round floats, sort dict keys."""
    if isinstance(value, float):
        return round(value, round_floats)
    if isinstance(value, dict):
        return {k: normalize(v, round_floats) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize(v, round_floats) for v in value]
    return value


def dump(label: str, value) -> None:
    print(f"=== {label} ===")
    print(json.dumps(normalize(value), ensure_ascii=False, indent=2))


def main() -> int:
    from hermes_cli.plugins import discover_plugins

    discover_plugins()  # idempotent; the same call a session start performs
    from tools.mcp_tool_discovery import discover_mcp_tools

    names = sorted(discover_mcp_tools())
    memoryweb_tools = [n for n in names if "memoryweb" in n]
    dump("surface", {"mcp_tools_total": len(names), "all_mcp_tools": names,
                     "memoryweb_tool_count": len(memoryweb_tools),
                     "memoryweb_tools": memoryweb_tools})

    if "--calls" not in sys.argv:
        return 0
    if not memoryweb_tools:
        print("=== calls ===\nskipped: no memoryweb tools registered", file=sys.stderr)
        return 1

    from model_tools import handle_function_call

    def call(tool: str, args: dict):
        reply = json.loads(handle_function_call(tool, args))
        # tool replies arrive wrapped as {"result": "<text>"} or
        # {"content":[{"type":"text","text":JSON}]}; unwrap the inner text
        # whenever it is itself JSON.
        for envelope in ("result",):
            if isinstance(reply, dict) and isinstance(reply.get(envelope), str):
                try:
                    reply = json.loads(reply[envelope])
                except json.JSONDecodeError:
                    return reply[envelope]
                break
        if isinstance(reply, dict) and isinstance(reply.get("content"), list):
            texts = [c.get("text", "") for c in reply["content"] if isinstance(c, dict)]
            try:
                return json.loads("".join(texts))
            except (json.JSONDecodeError, TypeError):
                return "".join(texts)
        return reply

    # derive the provider prefix from a registered name: "mcp__memoryweb__audit"
    # -> "mcp__memoryweb__" (never assume the alphabetical first tool's verb)
    parts = memoryweb_tools[0].split("__")
    prefix = "__".join(parts[:2]) + "__"
    domains = call(f"{prefix}domains", {"action": "list"})
    dump("call:domains", domains)

    search = call(f"{prefix}search", {"query": "passthrough", "limit": 3})
    dump("call:search", search)

    first_id = None
    if isinstance(search, dict):
        for node in search.get("nodes", []):
            if isinstance(node, dict) and node.get("id"):
                first_id = node["id"]
                break
    if first_id:
        dump("call:recall", call(f"{prefix}recall", {"id": first_id}))
    else:
        print("=== call:recall ===\nskipped: no node id from search")
    return 0


if __name__ == "__main__":
    sys.exit(main())
