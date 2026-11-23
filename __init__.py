"""memoryweb memory provider — bridges the local memoryweb graph server into
Hermes as a native ``MemoryProvider`` (``memory.provider: memoryweb``).

Layout: directory provider (``$HERMES_HOME/plugins/memoryweb/``). All state is
bound per instance at ``initialize()`` from the caller's ``hermes_home`` — no
module-level mutable state (multiplexing contract, #140058).

What each surface does:

- tools: the server's full tool set (fetched live at initialize via MCP
  ``tools/list``) is exposed as ``memoryweb_<tool>`` OpenAI schemas and
  dispatched verbatim through a long-lived stdio JSON-RPC connection;
- system_prompt_block: a static pointer block plus the live domain snapshot
  captured once at initialize (recalled context rides prefetch, not here);
- prefetch/queue_prefetch: background semantic search after each turn,
  cached and returned as recall context for the next turn;
- sync_turn: one transient session-trace node per session (upsert, never
  per-turn writes) so the graph sees session activity without transcript spam;
- on_pre_compress: checkpoint API v2 — a durable single-file ``memoryweb
  backup`` snapshot is committed under ``memoryweb-checkpoints/`` next to the
  database BEFORE returning; any failure raises (fail-closed) so a required
  checkpoint blocks the lossy rewrite instead of silently skipping.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import (
    PRE_COMPRESS_CHECKPOINT_API_VERSION,
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)

TOOL_PREFIX = "memoryweb_"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TRACE_DOMAIN = "hermes/sessions"
DEFAULT_MIRROR_DOMAINS = {"memory": "hermes/memory", "user": "user-profile"}
_CHECKPOINT_DIRNAME = "memoryweb-checkpoints"

# Graph-domain overrides (unset = defaults above). Env-based so any deployment
# can fold mirrors into existing domains or isolate multi-instance hosts; note
# the environment is process-wide, so a multiplexed gateway serving several
# profiles shares one mapping — per-profile splits need separate processes (#8).
_DOMAIN_ENV = {
    "memory": "MEMORYWEB_DOMAIN_MEMORY",
    "user": "MEMORYWEB_DOMAIN_USER",
    "sessions": "MEMORYWEB_DOMAIN_SESSIONS",
}

# Frozen tool table (name/description/inputSchema from memoryweb v1.65.0's
# tools/list, see static_tools.json). MemoryManager builds its dispatch routing
# from get_tool_schemas() ONCE, at provider registration — which happens before
# any session initialize() could start the stdio server. Returning this table
# until the live tool list arrives keeps registration-time routing complete
# ("registered (0 tools)" + later "Unknown tool" otherwise, #3). Live schemas
# replace it after initialize; a server tool absent here cannot be routed until
# the provider is re-registered — regenerate the table when bumping the pinned
# memoryweb version.
_STATIC_TOOLS_PATH = Path(__file__).resolve().parent / "static_tools.json"


def _load_static_tools() -> list[dict[str, Any]]:
    try:
        data = json.loads(_STATIC_TOOLS_PATH.read_text(encoding="utf-8"))
        return [t for t in data.get("tools", []) if isinstance(t, dict) and t.get("name")]
    except (OSError, ValueError):
        return []


def _package_version() -> str:
    """Single version source: plugin.json in this plugin directory (#7).

    Every surface that needs a version string (MCP clientInfo, logs, probes)
    reads it from here; release = bump plugin.json + tag, no other edits.
    """
    try:
        data = json.loads((Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "0")
    except (OSError, ValueError):
        return "0"


PROVIDER_VERSION = _package_version()

# Environment variables forwarded to the server process when set in the
# provider's own environment (never required; the server has its own defaults).
# Presence-based: an explicitly-empty MEMORYWEB_OLLAMA_API_KEY forwards as "",
# preserving the server's opt-out (suppress the OLLAMA_API_KEY fallback).
_PASSTHROUGH_ENV = (
    "OLLAMA_HOST",
    "MEMORYWEB_EMBED_MODEL",
    "OLLAMA_API_KEY",
    "MEMORYWEB_OLLAMA_API_KEY",
)

# Resolution order for the server binary: explicit override, PATH, then
# well-known install locations. Gateway cold start can run the first agent
# init before the boot-time PATH augmentation reaches this process (#4), so
# PATH alone is not a stable contract at that moment; the fallbacks make
# availability independent of when the provider is first asked.
_COMMAND_CANDIDATES = (
    "~/.local/bin/memoryweb",
    "/usr/local/bin/memoryweb",
    "/opt/mcp/memoryweb/memoryweb",
)


def _resolve_command() -> str:
    override = os.environ.get("MEMORYWEB_BIN", "").strip()
    if override:
        expanded = os.path.expanduser(override)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        return ""
    found = shutil.which("memoryweb")
    if found:
        return found
    for candidate in _COMMAND_CANDIDATES:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return ""


class _StdioServer:
    """One long-lived, thread-safe JSON-RPC conversation with the memoryweb
    stdio server (one message per line — same transport as tests/mcp_min_client.py,
    vendored here so the provider stays a single self-contained file)."""

    def __init__(self, command: str, env: dict[str, str], timeout: float = 60.0):
        self._argv = [command]
        self._env = env
        self._timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self._env, text=True, encoding="utf-8",
            bufsize=1,
        )
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "hermes-memoryweb-provider", "version": PROVIDER_VERSION},
        })
        self._notify("notifications/initialized")

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # -- transport ---------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, request_id: int) -> Any:
        import selectors
        assert self._proc is not None and self._proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self._proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                if not selector.select(self._timeout):
                    raise TimeoutError(f"memoryweb server: no response to request {request_id}")
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("memoryweb server closed stdout")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == request_id and ("result" in message or "error" in message):
                    if "error" in message:
                        raise RuntimeError(f"memoryweb server error: {message['error']}")
                    return message["result"]
        finally:
            selector.unregister(self._proc.stdout)
            selector.close()

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return self._read_response(request_id)

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    # -- MCP surface -------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return (self._request("tools/list", {}) or {}).get("tools", [])

    def call(self, tool: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        if isinstance(result, dict) and result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
            raise RuntimeError(f"memoryweb tool '{tool}' failed: {' '.join(texts)}")
        return result

    @staticmethod
    def result_text(result: Any) -> str:
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            return "".join(c.get("text", "") for c in result["content"] if isinstance(c, dict))
        return json.dumps(result, ensure_ascii=False)


def register(ctx) -> None:
    """Directory-provider entry point (``plugins/memory`` loader contract)."""
    ctx.register_memory_provider(MemorywebMemoryProvider())


class MemorywebMemoryProvider(MemoryProvider):
    """MemoryProvider backed by a local memoryweb stdio server."""

    # Every successful on_pre_compress() return means a durable checkpoint is
    # committed on disk (raise on any failure — never partial success).
    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self) -> None:
        self._server: Optional[_StdioServer] = None
        self._command = ""
        self._db_path = ""
        self._session_id = ""
        self._platform = ""
        self._agent_context = "primary"
        self._hermes_home = ""
        self._prompt_block = ""
        self._tools: list[dict[str, Any]] = []
        self._trace_label = ""
        self._trace_domain = DEFAULT_TRACE_DOMAIN
        self._domains = dict(DEFAULT_MIRROR_DOMAINS)
        self._turn_count = 0
        self._prefetch_cache: dict[str, str] = {}
        self._last_recall_count = 0

    # -- core lifecycle ------------------------------------------------------

    @property
    def name(self) -> str:
        return "memoryweb"

    def is_available(self) -> bool:
        """Binary resolvable (override/PATH/known locations); no network, no DB touch."""
        return bool(_resolve_command())

    def unavailable_reason(self) -> str:
        return ("memoryweb binary not found — set MEMORYWEB_BIN, put it on PATH, or "
                "install it under ~/.local/bin, /usr/local/bin or /opt/mcp/memoryweb "
                "to activate this provider.")

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = str(kwargs.get("hermes_home") or "")
        if not hermes_home:
            raise RuntimeError("memoryweb provider: initialize() requires hermes_home")
        command = _resolve_command()
        if not command:
            raise RuntimeError(self.unavailable_reason())
        self._command = command
        self._hermes_home = hermes_home
        self._session_id = session_id
        self._platform = str(kwargs.get("platform") or "")
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._db_path = os.environ.get("MEMORYWEB_DB") or str(Path(hermes_home) / ".memoryweb.db")
        self._trace_label = f"session-trace:{session_id[:16]}"

        child_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "MEMORYWEB_DB": self._db_path,
        }
        for var in _PASSTHROUGH_ENV:
            if var in os.environ:  # presence-based: explicit "" forwards as ""
                child_env[var] = os.environ[var]

        # graph-domain overrides, resolved per instance at initialize (#8)
        self._domains = dict(DEFAULT_MIRROR_DOMAINS)
        for target, env_name in _DOMAIN_ENV.items():
            value = os.environ.get(env_name, "").strip()
            if value:
                if target == "sessions":
                    self._trace_domain = value
                else:
                    self._domains[target] = value

        self._server = _StdioServer(command, child_env)
        self._server.start()
        self._tools = self._server.list_tools()
        if not self._tools:
            raise RuntimeError("memoryweb provider: server exposed no tools")
        self._prompt_block = self._build_prompt_block()

    def shutdown(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()

    # -- MCP dispatch helper ---------------------------------------------------

    def _call(self, tool: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        server = self._server
        if server is None:
            raise RuntimeError("memoryweb provider is not initialized (server closed)")
        return server.call(tool, arguments)

    # -- prompt + recall -----------------------------------------------------

    def _build_prompt_block(self) -> str:
        domains: list[str] = []
        try:
            payload = json.loads(_StdioServer.result_text(self._call("domains", {"action": "list"})))
            domains = [str(d) for d in (payload.get("domains") or [])]
        except Exception:
            pass
        snapshot = ", ".join(domains[:12]) if domains else "empty graph"
        more = f" (+{len(domains) - 12} more)" if len(domains) > 12 else ""
        return (
            "## memoryweb memory graph\n\n"
            "Long-term memory lives in a local memoryweb graph (SQLite with semantic search), "
            "not in this prompt. Live domains at session start: "
            f"{snapshot}{more}.\n\n"
            "Use the memoryweb_* tools as the memory surface: search before filing anything, "
            "file with remember only what is durable, connect related nodes, and let transient "
            "session state stay transient. Recalled context arrives with your turns; do not "
            "re-read the whole graph."
        )

    def system_prompt_block(self) -> str:
        return self._prompt_block

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if is_trivial_prompt(query):
            return
        spawn_context_thread(
            lambda: self._run_prefetch(query), name="memoryweb-prefetch").start()

    def _run_prefetch(self, query: str) -> None:
        try:
            text = _StdioServer.result_text(
                self._call("search", {"query": query, "limit": 5, "digest": True}))
            self._prefetch_cache[query] = self._format_recall(text)
        except Exception:
            self._prefetch_cache.pop(query, None)

    @staticmethod
    def _format_recall(digest_text: str) -> str:
        return f"[memoryweb recall]\n{digest_text}" if digest_text.strip() else ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._prefetch_cache.pop(query, "")

    def recall_status(self) -> Optional[RecallStatus]:
        return RecallStatus(provider_label="memoryweb", count=self._last_recall_count)

    # -- write path ----------------------------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._agent_context != "primary":
            return  # subagents, cron and flush contexts skip writes
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            self._trace_label = f"session-trace:{session_id[:16]}"
            self._turn_count = 0
        self._turn_count += 1
        count, label = self._turn_count, self._trace_label
        spawn_context_thread(
            lambda: self._upsert_trace(label, count), name="memoryweb-sync-turn").start()

    def _upsert_trace(self, label: str, turn_count: int) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        description = (
            f"Automatic provider trace: {self._platform or 'hermes'} session "
            f"{self._session_id}, turn {turn_count}, last activity {stamp}. "
            "Written by the memoryweb memory provider's sync_turn."
        )
        try:
            found = json.loads(_StdioServer.result_text(
                self._call("search", {"query": label, "exact": True, "limit": 1})))
            nodes = found.get("nodes") if isinstance(found, dict) else None
            if nodes:
                self._call("revise", {
                    "id": nodes[0]["id"], "description": description})
            else:
                self._call("remember", {
                    "domain": self._trace_domain, "label": label,
                    "description": description,
                    "why_matters": "Session activity visibility for history/dream cycles.",
                    "node_kind": "transient", "tags": "session-trace auto"})
        except Exception:
            pass  # trace writes are best-effort; memory ops ride the tools

    def on_session_switch(
        self, new_session_id: str, *, parent_session_id: str = "",
        reset: bool = False, rewound: bool = False, **kwargs,
    ) -> None:
        self._session_id = new_session_id
        self._trace_label = f"session-trace:{new_session_id[:16]}"
        self._turn_count = 0
        self._prefetch_cache.clear()

    # -- compression checkpoint (fail-closed v2) ------------------------------

    def on_pre_compress(self, messages: list[dict[str, Any]], *, require_checkpoint: bool = False) -> str:
        # memoryweb's `backup` requires the destination to sit in the same
        # directory as the source database, so stage the snapshot there and
        # atomically move it into the checkpoint directory afterwards (the
        # completed snapshot is standalone — safe to move; the live pair is not).
        db_dir = Path(self._db_path).expanduser().resolve().parent
        checkpoint_dir = db_dir / _CHECKPOINT_DIRNAME
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        staging = db_dir / f".memoryweb-checkpoint-{stamp}-{int(time.time())}.db"
        destination = checkpoint_dir / f"pre-compress-{stamp}-{int(time.time())}.db"
        # Durable single-file snapshot via the server binary's own backup
        # subcommand (consistent copy, safe while the server is running).
        completed = subprocess.run(
            [self._command, "backup", "-db", self._db_path, str(staging)],
            capture_output=True, text=True, timeout=120)
        if completed.returncode != 0 or not staging.is_file():
            raise RuntimeError(
                f"memoryweb checkpoint failed (rc={completed.returncode}): "
                f"{(completed.stderr or completed.stdout or '')[-400:]}")
        try:
            os.replace(staging, destination)  # same directory -> same filesystem
        except OSError:
            destination = staging  # durable where it is; never claim a lost path
        return (
            f"memoryweb checkpoint committed before compression: session "
            f"{self._session_id}, {len(messages)} messages archived-context at "
            f"{destination.name}. Graph memory is unaffected by the rewrite."
        )

    # -- built-in write mirroring (on_memory_write) ----------------------------

    @staticmethod
    def _mirror_label(target: str, content: str) -> str:
        first_line = next((ln.strip() for ln in (content or "").splitlines() if ln.strip()), "")
        return f"hermes-{target}: {first_line[:60] or '(empty)'}"

    def _find_mirror(self, label: str) -> Optional[dict[str, Any]]:
        found = json.loads(_StdioServer.result_text(
            self._call("search", {"query": label, "exact": True, "limit": 1})))
        nodes = found.get("nodes") if isinstance(found, dict) else None
        return nodes[0] if nodes else None

    def on_memory_write(
        self, action: str, target: str, content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Mirror a built-in memory-tool write into the graph (best-effort).

        The built-in file store stays the source of truth (the ABC defines this
        hook as a mirror notification, fired after the file write succeeded);
        this keeps the graph in step so graph-side search/recall see curated
        facts too. remove resolves the prior entry via
        ``metadata["previous_content"]``.
        """
        if self._agent_context != "primary":
            return
        meta = dict(metadata or {})
        act, tgt, body = action, target, content or ""
        spawn_context_thread(
            lambda: self._mirror_write(act, tgt, body, meta),
            name="memoryweb-mirror-write").start()

    def _mirror_write(self, action: str, target: str, content: str, metadata: dict[str, Any]) -> None:
        domain = self._domains.get(target, DEFAULT_MIRROR_DOMAINS["memory"])
        try:
            if action == "remove":
                previous = metadata.get("previous_content") or ""
                node = self._find_mirror(self._mirror_label(target, previous or content))
                if node:
                    self._call("forget", {
                        "id": node["id"],
                        "reason": f"mirrored built-in '{target}' entry removed via on_memory_write",
                    })
                return
            label = self._mirror_label(target, content)
            previous = metadata.get("previous_content") or ""
            prior = (self._find_mirror(self._mirror_label(target, previous))
                     if previous else None) or self._find_mirror(label)
            if prior:
                # same curated fact, new wording: revise in place, label included,
                # so a replace never orphans the earlier mirror
                self._call("revise", {"id": prior["id"], "label": label,
                                      "description": content})
            else:
                self._call("remember", {
                    "domain": domain, "label": label, "description": content,
                    "why_matters": "Mirrored from the built-in memory tool "
                                   "(on_memory_write); the file store remains authoritative.",
                    "node_kind": "reference", "tags": f"builtin-mirror {target}",
                })
        except Exception:
            pass  # mirror is best-effort; a failed mirror never blocks the file write

    # -- tools -----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        source = self._tools if self._tools else _load_static_tools()
        schemas = []
        for tool in source:
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            schemas.append({
                "name": f"{TOOL_PREFIX}{name}",
                "description": str(tool.get("description") or name)[:1024],
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            })
        return schemas

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if not tool_name.startswith(TOOL_PREFIX):
            return json.dumps({"error": f"unknown tool {tool_name}"})
        server_tool = tool_name[len(TOOL_PREFIX):]
        try:
            result = self._call(server_tool, args)
            text = _StdioServer.result_text(result)
            try:
                return json.dumps({"result": json.loads(text)}, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"result": text}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)


# ── Background-review prompt override ────────────────────────────────────────
#
# agent/background_review.py picks each review fork's prompt with
# ``getattr(agent, name, globals()[name])`` on the SESSION agent: instance dict
# first, then the AIAgent class attribute (run_agent.py imports the three
# module constants into the class body), then the module fallback. The class
# attribute is the surface a plugin can reach — no plugin seam receives the
# session agent instance itself — and setattr on it is picked up by every
# agent's getattr, while a per-instance setattr can still shadow it.
#
# Swapped only when the plugin's memory face is active (memory.provider names
# this plugin, or a memoryweb MCP server is registered); otherwise the official
# prompts stay untouched. All imports are lazy and every step fails silent back
# to the official prompts — the review loop must never die on this hook.
#
# Note for operators: the review fork dispatches behind
# ``auxiliary.background_review.extra_tools`` — list the memoryweb tool names
# there so the fork may actually call what these prompts direct it to use.

_MEMORYWEB_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider filing durable facts into memory if appropriate.\n\n"
    "Memory lives behind the memoryweb tools — read with memoryweb_search / memoryweb_recall / "
    "memoryweb_orient, write with memoryweb_remember / memoryweb_revise / memoryweb_connect. "
    "File findings there; do not write USER.md or MEMORY.md.\n\n"
    "• SEARCH BEFORE FILING: recall what already exists for the subject. Strengthen or clarify "
    "the existing entry (memoryweb_revise) instead of appending a duplicate (memoryweb_remember); "
    "the update-or-create call is yours to make. Connect genuinely related entries "
    "(memoryweb_connect) rather than restating one inside the other.\n"
    "• ONE fact goes in ONE place — never both a new entry and an edit of an old one carrying "
    "the same fact. Duplicates crowd out what matters and split future searches.\n"
    "• Save only facts GROUNDED in this conversation — a user message or tool result you can "
    "point to. Never infer the user's traits, skill level, role, mood or preferences from topic, "
    "tone or tools used, and never turn a one-off request into a standing preference unless the "
    "user marked it general ('always', 'from now on', 'remember') or stated a standing "
    "like/dislike. If unsure, leave it out — a wrong fact misleads every future session.\n"
    "• Both stores of old are one surface now: who the user is (persona, preferences, "
    "communication and work style) and durable environment facts (tool quirks, project "
    "conventions, config gotchas, paths and endpoints) all file through the same tools — pick "
    "the fitting domain when you file.\n\n"
    "If something stands out, file it once, in the right place, with the memoryweb tools. "
    "If nothing is worth filing, just say 'Nothing to save.' and stop."
)

_MEMORYWEB_SKILLS_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be ACTIVE — most sessions "
    "produce at least one skill update, even if small. A pass that does nothing is a missed "
    "learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a SKILL.md of always-on rules "
    "and a small references/ set of topical depth — not a flat list of narrow one-session "
    "skills, and not an umbrella hoarding a references/ file per session. This shapes HOW you "
    "update, not WHETHER you update.\n\n"
    "What a skill IS: the instructions for doing a class of task the most efficient and correct "
    "way, to THIS user's specifications — the procedure, the tools and commands that work, the "
    "order, the user's preferences for how the result should look, and the pitfalls that cost "
    "time. A future session should be able to follow it and produce what the user wants on the "
    "first try.\n"
    "• Procedure first: steps in the order they are done, with the concrete commands, tool "
    "calls, and decision points. Pitfalls attach to the step they affect; a pitfall is a "
    "generalizable rule + one clause of WHY, imperative — not a narrative of this session.\n"
    "• No PR/issue numbers, dates, ticket IDs or quoted user text as content — the rule must "
    "stand without the incident behind it.\n"
    "• The same lesson learned twice is ONE rule: search the skill (and its references/) for "
    "the rule already stated and strengthen it rather than appending a second copy. Fix the "
    "skill in place when it is wrong — edit the sentence that misled, do not append "
    "'UPDATE: actually...' underneath it.\n\n"
    "Signals to look for (any one warrants action):\n"
    "• User corrected your style, tone, format, legibility, verbosity, workflow, or sequence of "
    "steps — frustration signals like 'stop doing X', 'this is too verbose', 'just give me the "
    "answer', or an explicit 'remember this'. Encode the correction in the skill that governs "
    "that class of task so the next session starts already knowing.\n"
    "• Non-trivial technique, fix, workaround, debugging path, or tool-usage pattern emerged "
    "that a future session would benefit from.\n"
    "• A skill loaded or consulted this session turned out wrong, missing a step, or outdated — "
    "patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits:\n"
    "1. UPDATE A CURRENTLY-LOADED SKILL (re-load it with skill_view during this review — see "
    "Read-before-write). Whoever wrote it: hand-written, installed, bundled and pinned skills "
    "are all yours to improve; only a skill the user LOCKED (`hermes curator lock`) is off "
    "limits — put the suggested change in your summary instead.\n"
    "2. UPDATE AN EXISTING UMBRELLA (skills_list + skill_view), adding a subsection, a pitfall, "
    "or a broader trigger.\n"
    "3. ADD A SUPPORT FILE under an existing umbrella via skill_manage action=write_file — "
    "references/<topic>.md for topical depth (extend an existing file first, never a per-session "
    "file), templates/ for starter files meant to be copied, scripts/ for statically re-runnable "
    "actions. Add a one-line pointer in SKILL.md so future agents find it.\n"
    "4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing covers the class. The name MUST be at "
    "the class level — not a PR number, error string, codename, or 'fix-X / debug-Y' session "
    "artifact; if it only fits today's task, fall back to (1)–(3).\n\n"
    "Read-before-write (ENFORCED — skill_manage refuses otherwise): before patching or editing "
    "an existing skill's SKILL.md, call skill_view(name) during this review; before overwriting "
    "or removing an EXISTING supporting file, call skill_view(name, file_path=...) for that "
    "exact file. Earlier transcript quotes do not count. On a refusal: view the named target "
    "once, retry the write once, do not loop.\n\n"
    "User-preference embedding: when the user expressed a style/format/workflow preference, the "
    "update belongs in the SKILL.md body of the skill governing that task. Cross-cutting "
    "preferences no skill owns go to memory via the memoryweb tools "
    "(memoryweb_remember / memoryweb_revise) — one place per lesson, never both. Skills say "
    "'how to do this class of task for this user'; memory says who the user is and what holds "
    "true of the environment.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed constraints that bite "
    "later): environment-dependent failures (missing binaries, unconfigured credentials — the "
    "user can fix these); negative claims about tools or features ('X is broken'); transient "
    "errors that resolved before the conversation ended; one-off task narratives; and "
    "unresolved failures — never dress up a sequence of dead ends as a 'reliable workflow'. If "
    "a tool failed on setup state, capture the FIX under an existing setup or troubleshooting "
    "skill, not the failure. If you notice two overlapping skills, note it in your reply — the "
    "background curator consolidates; never delete a skill here.\n\n"
    "'Nothing to save.' is a real option but should NOT be the default. If the session ran "
    "smoothly with no corrections and produced no new technique, just say 'Nothing to save.' "
    "and stop. Otherwise, act."
)

_MEMORYWEB_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things.\n\n"
    "**Memory**: lives behind the memoryweb tools — read with memoryweb_search / "
    "memoryweb_recall / memoryweb_orient, write with memoryweb_remember / memoryweb_revise / "
    "memoryweb_connect. File findings there; do not write USER.md or MEMORY.md.\n"
    "• SEARCH BEFORE FILING: recall what already exists; strengthen or clarify the existing "
    "entry (memoryweb_revise) instead of appending a duplicate (memoryweb_remember); the "
    "update-or-create call is yours to make. Connect genuinely related entries "
    "(memoryweb_connect) rather than restating one inside the other.\n"
    "• ONE fact goes in ONE place — never both a new entry and an edit of an old one carrying "
    "the same fact.\n"
    "• Save only facts GROUNDED in this conversation — a user message or tool result you can "
    "point to. Never infer the user's traits, skill level, role, mood or preferences from topic, "
    "tone or tools used, and never turn a one-off request into a standing preference unless the "
    "user marked it general ('always', 'from now on', 'remember'). If unsure, leave it out.\n"
    "• Who the user is (persona, preferences, work style) and durable environment facts (tool "
    "quirks, project conventions, config gotchas, paths, endpoints) all file through the same "
    "tools — pick the fitting domain when you file.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most sessions produce at least one "
    "skill update; a pass that does nothing is a missed learning opportunity, not a neutral "
    "outcome. Target shape: CLASS-LEVEL skills with a SKILL.md of always-on rules and a small "
    "references/ set of topical depth. Procedure first, pitfalls as generalizable rules with "
    "one clause of WHY; no PR numbers, dates or incident narration; the same lesson learned "
    "twice is ONE rule — strengthen, don't duplicate; fix wrong sentences in place.\n\n"
    "Signals (either dimension): user corrected style/tone/format/verbosity/approach — embed it "
    "in the skill governing that task so the next session starts fixed; a non-trivial "
    "technique, fix, workaround or debugging path emerged; a loaded skill turned out wrong, "
    "missing or outdated — patch it now; a durable grounded fact about the user or environment "
    "surfaced — file it into memory.\n\n"
    "Preference order for skills — earliest that fits: (1) update a currently-loaded skill "
    "(re-load with skill_view first — read-before-write is ENFORCED, one view + one retry on "
    "refusal, no loop); (2) update an existing umbrella (skills_list + skill_view); (3) add a "
    "support file under an umbrella via skill_manage action=write_file (references/ for depth, "
    "templates/ for starters, scripts/ for re-runnable actions; one-line pointer in SKILL.md); "
    "(4) create a new class-level umbrella only when nothing covers the class — the name must "
    "sit at the class level, never a session artifact. Locked skills are off limits; suggest "
    "the change in your summary instead. Never delete a skill; note overlaps for the curator.\n\n"
    "User-preference embedding: a preference lesson lives in exactly ONE place — the skill that "
    "governs the task when one exists; cross-cutting preferences no skill owns go to memory via "
    "the memoryweb tools. Never both.\n\n"
    "Do NOT capture as skills: environment-dependent failures (missing binaries, unconfigured "
    "credentials), negative claims about tools ('X is broken'), transient errors that resolved, "
    "one-off task narratives, unresolved failure sequences dressed up as workflows — capture the "
    "FIX under an existing setup skill, not the failure.\n\n"
    "Act on whichever dimension has real signal. If genuinely nothing stands out on either, say "
    "'Nothing to save.' and stop — but don't reach for that conclusion as a default."
)

# AIAgent class-attribute names. NOTE the runtime's own naming is
# inconsistent across versions: the scope map looks up "_SKILLS_REVIEW_PROMPT"
# (plural) while run_agent.py imports the module constant as
# "_SKILL_REVIEW_PROMPT" (singular) into the class — so both spellings are
# installed to cover either lookup.
_REVIEW_PROMPT_OVERRIDES = {
    "_MEMORY_REVIEW_PROMPT": _MEMORYWEB_MEMORY_REVIEW_PROMPT,
    "_SKILLS_REVIEW_PROMPT": _MEMORYWEB_SKILLS_REVIEW_PROMPT,
    "_SKILL_REVIEW_PROMPT": _MEMORYWEB_SKILLS_REVIEW_PROMPT,
    "_COMBINED_REVIEW_PROMPT": _MEMORYWEB_COMBINED_REVIEW_PROMPT,
}


def _memoryweb_memory_face_active() -> bool:
    """True when this plugin's memory face is in play for the process.

    memory.provider naming this plugin (the provider face) or a registered
    memoryweb MCP server (the portable face) both count: either way the
    session surface carries the memoryweb tools these prompts direct to.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return False
    memory = cfg.get("memory")
    if isinstance(memory, dict) and str(memory.get("provider") or "").strip().lower() == "memoryweb":
        return True
    mcp = cfg.get("mcp_servers")
    return isinstance(mcp, dict) and "memoryweb" in mcp


def _install_review_prompt_overrides() -> list:
    """Swap the AIAgent review-prompt class attributes to the memoryweb texts.

    Returns the attribute names actually installed (diagnostics/testing).
    Fail-silent per attribute: an AttributeError keeps the remaining swaps and
    the official prompt for the missing name.
    """
    try:
        from run_agent import AIAgent
    except Exception:
        return []
    installed = []
    for name, text in _REVIEW_PROMPT_OVERRIDES.items():
        try:
            setattr(AIAgent, name, text)
            installed.append(name)
        except Exception:
            continue
    return installed


def register(ctx) -> dict:
    """Native-plugin registration hook (PluginManager calls this at load).

    Installs the background-review prompt override when the memory face is
    active. Idempotent; returns a small diagnostics dict for tests and logs.
    """
    active = _memoryweb_memory_face_active()
    installed = _install_review_prompt_overrides() if active else []
    return {"memory_face_active": active, "review_prompts_installed": installed}
