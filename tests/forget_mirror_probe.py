#!/usr/bin/env python3
"""Acceptance probe for the forget-mirror sync (iss #15).

The write direction of the mirror (on_memory_write: built-in memory-tool
writes -> graph nodes) has had an acceptance section in provider_probe.py
since day one; the delete direction (graph-side forget -> file entry removed)
is the surface this probe exercises:

  - forgetting a builtin-mirror node removes its MEMORY.md / USER.md entry
    through the NATIVE store (lock + re-read + unique-match), so non-mirror
    entries in the same file survive byte-for-byte;
  - a plain (non-mirror) graph node's forget never touches the files;
  - a stale mirror (file entry edited after the last mirror sync) is REFUSED,
    not deleted;
  - restore=true is an un-archive and never plans a deletion;
  - non-primary contexts (review forks, cron) skip the file sync — the same
    posture as the host's own unattended-delete gate;
  - forget_all syncs every mirror id in the batch (below the cap).

Runs inside a Hermes environment (the runtime's python) against an ISOLATED
home: a throwaway HERMES_HOME + MEMORYWEB_DB, with the provider module loaded
straight from this repository checkout:

    PYTHONPATH=<hermes runtime checkout> \
    python3 tests/forget_mirror_probe.py

Exit 0 = all checks green.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "memoryweb_plugin_repo", str(REPO_ROOT / "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fresh_entries():
    """Entries as the native store parses them right now (fresh disk read)."""
    from tools.memory_tool_store import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    return store.memory_entries, store.user_entries


def wait_for_mirror(provider, label: str, timeout: float = 15.0):
    """Poll for the mirror node (the on_memory_write thread is async)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        reply = json.loads(provider.handle_tool_call(
            "memoryweb_search", {"query": label, "exact": True, "limit": 1}))
        nodes = (reply.get("result") or {}).get("nodes") or []
        if nodes:
            return nodes[0]
        time.sleep(1.0)
    return None


def mirror_fact(provider, target: str, content: str):
    """Mirror one built-in write into the graph and return the mirror node."""
    provider.on_memory_write("add", target, content, None)
    return wait_for_mirror(provider, f"hermes-{target}: {content[:60]}")


def main() -> int:
    if shutil.which("memoryweb") is None and not os.path.exists(os.path.expanduser("~/.local/bin/memoryweb")):
        print("memoryweb binary not on PATH — run inside a Hermes environment")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="forget-mirror-probe-"))
    home, db_path = tmp / "home", tmp / "memoryweb.db"
    home.mkdir(parents=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["MEMORYWEB_DB"] = str(db_path)

    module = load_provider_module()
    check("plugin module loads from the repo checkout",
          hasattr(module, "MemorywebMemoryProvider"))

    # -- pure-function guards (no server needed) -----------------------------
    classify = module.MemorywebMemoryProvider._classify_mirror_node
    check("classify: real mirror node (memory)",
          classify({"tags": "builtin-mirror memory", "label": "hermes-memory: x"}) == "memory")
    check("classify: real mirror node (user)",
          classify({"tags": "builtin-mirror user", "label": "hermes-user: x"}) == "user")
    check("classify: missing tag token -> None",
          classify({"tags": "memory", "label": "hermes-memory: x"}) is None)
    check("classify: missing label prefix -> None",
          classify({"tags": "builtin-mirror memory", "label": "some node"}) is None)
    check("classify: plain graph node -> None",
          classify({"tags": "", "label": "decision: thing"}) is None)
    check("classify: non-dict -> None", classify(None) is None)

    long_items = [{"id": f"n{i}", "reason": "cap"} for i in range(51)]
    provider = module.MemorywebMemoryProvider()
    provider.initialize("forget-probe", hermes_home=str(home), platform="probe",
                        agent_context="primary")
    check("cap: forget_all over _MIRROR_SYNC_BATCH_CAP plans nothing",
          provider._plan_forget_sync("forget_all", {"items": long_items}) == [])
    check("restore=true plans nothing",
          provider._plan_forget_sync("forget", {"id": "x", "restore": True}) == [])

    # -- seed the native files through the store's own writes ----------------
    from tools.memory_tool_store import MemoryStore
    seeder = MemoryStore()
    keeper = "Unrelated keeper fact: the CI suite runs on weekday evenings."
    fact_m = "Probe mirror fact: the deploy window opens after the checkpoint passes."
    fact_u = "Probe profile fact: the user signs releases with the household key."
    curly = "Probe typographic fact: the “signed” ledger uses real dash — marks."
    straight = "Probe typographic fact: the 'signed' ledger uses real dash - marks."
    seeder.add("memory", keeper)
    seeder.add("memory", fact_m)
    seeder.add("user", fact_u)
    seeder.add("memory", "Probe drift fact: the rotation key changes on Mondays.")
    seeder.add("memory", curly)
    check("seed: native store holds 4 memory entries + 1 user entry",
          len(fresh_entries()[0]) == 4 and len(fresh_entries()[1]) == 1)

    # -- 1) plain forget of a mirror node removes its MEMORY.md entry --------
    node = mirror_fact(provider, "memory", fact_m)
    check("mirror node landed in the graph", node is not None,
          str((node or {}).get("id")))
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": node["id"], "reason": "probe: forget sync"}))
    sync = reply.get("mirror_sync") or []
    check("forget reply reports a removed MEMORY.md sync",
          sync == [{"target": "memory", "status": "removed", "file": "MEMORY.md"}],
          json.dumps(sync, ensure_ascii=False)[:120])
    mem_entries, user_entries = fresh_entries()
    check("MEMORY.md entry removed by the forget", fact_m not in mem_entries)
    check("non-mirror keeper entry intact", keeper in mem_entries)
    check("USER.md untouched by a memory-target forget", user_entries == [fact_u])

    # -- 2) same for the user target -----------------------------------------
    node_u = mirror_fact(provider, "user", fact_u)
    check("user-target mirror node landed", node_u is not None)
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": node_u["id"], "reason": "probe: forget sync"}))
    check("forget reply reports a removed USER.md sync",
          (reply.get("mirror_sync") or [{}])[0].get("status") == "removed"
          and (reply.get("mirror_sync") or [{}])[0].get("file") == "USER.md",
          json.dumps(reply.get("mirror_sync"), ensure_ascii=False)[:120])
    mem_entries, user_entries = fresh_entries()
    check("USER.md entry removed by the forget", fact_u not in user_entries)
    check("MEMORY.md untouched by a user-target forget",
          mem_entries == [keeper, "Probe drift fact: the rotation key changes on Mondays.", curly])

    # -- 3) a plain graph node's forget never touches the files --------------
    provider.handle_tool_call("memoryweb_remember", {
        "domain": "probe/forget-sync", "label": "plain node: not a mirror",
        "description": "Ordinary graph node with no builtin-mirror tag.",
        "why_matters": "probe fixture", "node_kind": "reference",
        "tags": "probe"})
    plain_node = wait_for_mirror(provider, "plain node: not a mirror")
    plain_id = (plain_node or {}).get("id")
    check("plain graph node created", bool(plain_id), str(plain_id))
    before = fresh_entries()
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": plain_id, "reason": "probe: plain forget"}))
    check("plain forget carries no mirror_sync key", "mirror_sync" not in reply,
          str(list(reply))[:80])
    check("files unchanged after a plain forget", fresh_entries() == before)

    # -- 4) stale mirror (file edited after the mirror) is refused -----------
    drifter = mirror_fact(provider, "memory",
                          "Probe drift fact: the rotation key changes on Mondays.")
    seeder2 = MemoryStore()
    patched = seeder2.patch("memory",
                            "the rotation key changes on Mondays",
                            "the rotation key changes on Tuesdays (amended)")
    check("probe fixture: file entry patched away from the mirror",
          isinstance(patched, dict) and patched.get("success"),
          json.dumps(patched, ensure_ascii=False)[:160] if isinstance(patched, dict) else str(patched))
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": drifter["id"], "reason": "probe: stale mirror"}))
    sync = reply.get("mirror_sync") or []
    check("stale mirror is refused, not deleted",
          sync and sync[0].get("status") == "refused",
          json.dumps(sync, ensure_ascii=False)[:120])
    mem_entries, _ = fresh_entries()
    check("amended file entry survives a stale-mirror forget",
          any("Tuesdays (amended)" in e for e in mem_entries))

    # -- 5) typography-folded content still resolves to its entry ------------
    node_c = mirror_fact(provider, "memory", straight)  # mirror holds straight text
    check("typography mirror node landed", node_c is not None)
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": node_c["id"], "reason": "probe: folded match"}))
    sync = reply.get("mirror_sync") or []
    check("fold-equal entry (curly in file, straight in mirror) removed",
          sync and sync[0].get("status") == "removed",
          json.dumps(sync, ensure_ascii=False)[:120])
    mem_entries, _ = fresh_entries()
    check("curly file entry gone after folded sync",
          not any("signed" in e and "ledger" in e for e in mem_entries),
          str(mem_entries))

    # -- 6) restore never deletes; re-forgetting an already-gone entry refuses
    before = fresh_entries()
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": node_u["id"], "restore": True}))
    check("restore reply carries no mirror_sync key", "mirror_sync" not in reply)
    check("files unchanged by a restore", fresh_entries() == before)
    restored = wait_for_mirror(provider, f"hermes-user: {fact_u[:60]}")
    check("restored mirror node is live again", restored is not None)
    reply = json.loads(provider.handle_tool_call(
        "memoryweb_forget", {"id": node_u["id"], "reason": "probe: entry already gone"}))
    sync = reply.get("mirror_sync") or []
    check("re-forget with no file entry left is refused",
          sync and sync[0].get("status") == "refused",
          json.dumps(sync, ensure_ascii=False)[:120])

    # -- 7) non-primary contexts skip the file sync ---------------------------
    sub = module.MemorywebMemoryProvider()
    sub.initialize("forget-probe-sub", hermes_home=str(home), platform="probe",
                   agent_context="subagent")
    gated = mirror_fact(provider, "memory",
                        "Probe gated fact: the subagent must not prune files.")
    before = fresh_entries()
    reply = json.loads(sub.handle_tool_call(
        "memoryweb_forget", {"id": gated["id"], "reason": "probe: subagent forget"}))
    sync = reply.get("mirror_sync") or []
    check("subagent forget notes a skipped sync",
          sync and sync[0].get("status") == "skipped"
          and "subagent" in sync[0].get("detail", ""),
          json.dumps(sync, ensure_ascii=False)[:120])
    check("file entry survives a subagent forget", fresh_entries() == before)
    gone = json.loads(sub.handle_tool_call(
        "memoryweb_search", {"query": f"hermes-memory: {gated['label'][14:][:46]}",
                             "exact": True, "limit": 1}))
    check("graph-side forget still committed for the subagent",
          not ((gone.get("result") or {}).get("nodes") or []))

    # -- 8) forget_all syncs every mirror in the batch ------------------------
    seeder3 = MemoryStore()
    seeded_a = seeder3.add("memory", "Probe batch fact A: first staged cleanup entry.")
    seeded_b = seeder3.add("user", "Probe batch fact B: first staged profile entry.")
    check("probe fixture: batch entries seeded",
          isinstance(seeded_a, dict) and seeded_a.get("success")
          and isinstance(seeded_b, dict) and seeded_b.get("success"),
          json.dumps([seeded_a, seeded_b], ensure_ascii=False)[:200])
    batch_a = mirror_fact(provider, "memory",
                          "Probe batch fact A: first staged cleanup entry.")
    batch_b = mirror_fact(provider, "user",
                          "Probe batch fact B: first staged profile entry.")
    mem_before, user_before = fresh_entries()
    reply = json.loads(provider.handle_tool_call("memoryweb_forget_all", {"items": [
        {"id": batch_a["id"], "reason": "probe: batch sync"},
        {"id": batch_b["id"], "reason": "probe: batch sync"}]}))
    sync = reply.get("mirror_sync") or []
    check("forget_all reports one sync per mirror",
          [n.get("status") for n in sync] == ["removed", "removed"]
          and {n.get("file") for n in sync} == {"MEMORY.md", "USER.md"},
          json.dumps(sync, ensure_ascii=False)[:160])
    mem_after, user_after = fresh_entries()
    check("forget_all pruned the MEMORY.md entry",
          len(mem_after) == len(mem_before) - 1
          and not any("batch fact A" in e for e in mem_after))
    check("forget_all pruned the USER.md entry",
          len(user_after) == len(user_before) - 1
          and not any("batch fact B" in e for e in user_after))
    check("non-mirror keeper entry still intact at the end",
          any(e == keeper for e in mem_after))

    provider.shutdown()
    sub.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("FORGET-MIRROR SYNC ACCEPTANCE GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
