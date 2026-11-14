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
