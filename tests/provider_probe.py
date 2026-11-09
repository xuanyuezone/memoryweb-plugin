#!/usr/bin/env python3
"""Acceptance probe for the memoryweb memory provider.

Loads the provider through Hermes's real discovery chain
(``plugins.memory.load_memory_provider`` → bundled ``plugins/memory/`` first,
then ``$HERMES_HOME/plugins/<name>/``), then exercises every implemented ABC
surface against a live memoryweb server. Exit 0 = all checks green.

Run inside a Hermes environment (the runtime's python), with the provider
copied to ``$HERMES_HOME/plugins/memoryweb/`` and the ``memoryweb`` binary on
PATH:

    HERMES_HOME=/opt/data python3 tests/provider_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import time

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from plugins.memory import find_provider_dir, load_memory_provider

    # 1) discovery chain: bundled-first, then the user plugins dir
    provider_dir = find_provider_dir("memoryweb")
    check("discovery: find_provider_dir resolves the user dir",
          provider_dir is not None and "plugins/memoryweb" in str(provider_dir),
          str(provider_dir))

    provider = load_memory_provider("memoryweb")
    check("loader: load_memory_provider returns an instance", provider is not None)
    if provider is None:
        return 1

    check("name property is 'memoryweb'", provider.name == "memoryweb")
    check("is_available (binary on PATH)", provider.is_available())

    # 1b) registration-time routing (issue #3): MemoryManager builds its
    # dispatch table from get_tool_schemas() ONCE at add_provider, before any
    # initialize() — the provider must answer with schemas at that point.
    from agent.memory_manager import MemoryManager

    manager = MemoryManager()
    manager.add_provider(provider)
    routed = len(getattr(manager, "_tool_to_provider", {}) or {})
    check("manager routing table filled at registration (pre-initialize)",
          routed >= 15, f"{routed} tools routed")

    # 2) initialize through the documented kwargs
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    session_id = f"provider-probe-{int(time.time())}"
    provider.initialize(session_id, hermes_home=hermes_home, platform="probe",
                        agent_context="primary")
    check("initialize: stdio server up, tools fetched", True)

    # 3) tool surface: live schemas + dispatch
    schemas = provider.get_tool_schemas()
    names = sorted(s["name"] for s in schemas)
    check(f"get_tool_schemas exposes the server tool set ({len(names)} tools)",
          len(names) >= 15 and all(n.startswith("memoryweb_") for n in names),
          ", ".join(names[:5]) + " …")

    reply = json.loads(provider.handle_tool_call("memoryweb_domains", {"action": "list"}))
    domains = (reply.get("result") or {}).get("domains") or []
    check("handle_tool_call dispatches to the live server",
          any(str(d).startswith("demo/") for d in domains), ", ".join(map(str, domains)))

    manager_reply = manager.handle_tool_call("memoryweb_domains", {"action": "list"})
    check("manager dispatch resolves memoryweb_* tools after initialize",
          "domains" in manager_reply, manager_reply[:70])

    bad = json.loads(provider.handle_tool_call("memoryweb_recall", {"id": "no-such-node"}))
    check("tool errors come back as JSON, never raise",
          "error" in bad or "result" in bad, str(bad)[:80])

    # 4) system prompt block: static pointer + domain snapshot
    block = provider.system_prompt_block()
    check("system_prompt_block carries the domain snapshot",
          "memoryweb" in block and "demo/" in block and len(block) < 2000,
          f"{len(block)} chars")

    # 5) prefetch path: background search cached, returned once
    provider.queue_prefetch("passthrough environment")
    time.sleep(3)
    recall = provider.prefetch("passthrough environment")
    check("queue_prefetch/prefetch return recall context",
          "memoryweb recall" in recall and "passthrough" in recall.lower(),
          recall.splitlines()[0] if recall else "(empty)")
    check("prefetch result is consumed (second call empty)",
          provider.prefetch("passthrough environment") == "")
    status = provider.recall_status()
    check("recall_status reports the provider label",
          status is not None and status.provider_label == "memoryweb")

    # 6) sync_turn: one transient session-trace node (upsert, async)
    provider.sync_turn("probe user turn", "probe assistant turn", session_id=session_id)
    time.sleep(3)
    found = json.loads(provider.handle_tool_call(
        "memoryweb_search", {"query": f"session-trace:{session_id[:16]}", "exact": True}))
    nodes = (found.get("result") or {}).get("nodes") or []
    check("sync_turn upserts a transient session-trace node", len(nodes) == 1,
          nodes[0]["id"] if nodes else "(none)")

    # 7) on_pre_compress: durable checkpoint, fail-closed
    db_path = os.environ.get("MEMORYWEB_DB") or os.path.join(hermes_home, ".memoryweb.db")
    checkpoint_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "memoryweb-checkpoints")
    before = set(os.listdir(checkpoint_dir)) if os.path.isdir(checkpoint_dir) else set()
    summary_line = provider.on_pre_compress([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])
    after = set(os.listdir(checkpoint_dir)) if os.path.isdir(checkpoint_dir) else set()
    new_files = sorted(after - before)
    check("on_pre_compress commits a durable snapshot before returning",
          bool(new_files) and "checkpoint" in summary_line,
          f"{new_files} | {summary_line[:70]}")
    check("checkpoint API version advertised as 2 (fail-closed)",
          getattr(provider, "pre_compress_checkpoint_api_version", 1) == 2)

    # 8) clean shutdown
    provider.shutdown()
    check("shutdown closes the server cleanly",
          getattr(provider, "_server", "missing") is None)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("PROVIDER ACCEPTANCE GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
