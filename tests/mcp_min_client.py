#!/usr/bin/env python3
"""Minimal, dependency-free MCP stdio client used by this repo's test fixtures.

Speaks the Model Context Protocol "stdio" transport directly: one JSON-RPC 2.0
message per line over the child process's stdin/stdout. It exists so the
acceptance harness can talk to a memoryweb server (list tools, call tools)
without requiring the `mcp` Python SDK — any CPython 3.9+ can run it, inside
or outside a Hermes installation.

Usage from other scripts:

    from mcp_min_client import McpStdioClient
    client = McpStdioClient("memoryweb", env={"MEMORYWEB_DB": "/tmp/demo.db"})
    client.start()
    tools = client.list_tools()
    result = client.call_tool("domains", {"action": "list"})
    client.close()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    """Raised when the server returns an error response."""


class McpStdioClient:
    """One JSON-RPC conversation with a stdio MCP server subprocess."""

    def __init__(self, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, timeout: float = 30.0):
        self.command = command
        self.args = list(args or [])
        # The child inherits a small, explicit environment (PATH, HOME and the
        # caller-provided variables) rather than this process's full environment.
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
        }
        child_env.update(env or {})
        self.env = child_env
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self.env,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "memory-plugin-min-client", "version": "0.1.0"},
        })
        self._notify("notifications/initialized")

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._request("shutdown", {}, expect_result=True, timeout=5.0)
            except Exception:
                pass
            try:
                self._notify("exit")
            except Exception:
                pass
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "McpStdioClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _send(self, payload: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, request_id: int, timeout: float) -> Dict[str, Any]:
        """Read lines until the response with *request_id* arrives.

        Server-initiated notifications and log messages are skipped. The MCP
        stdio transport never frames with Content-Length; each line is one
        message, and requests are answered strictly in order per connection.
        """
        assert self._proc is not None and self._proc.stdout is not None
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(self._proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                events = selector.select(timeout)
                if not events:
                    raise TimeoutError(f"no response to request {request_id} within {timeout}s")
                line = self._proc.stdout.readline()
                if not line:
                    stderr = ""
                    if self._proc.stderr is not None:
                        try:
                            stderr = self._proc.stderr.read() or ""
                        except Exception:
                            pass
                    raise McpError(f"server closed stdout (request {request_id}); stderr: {stderr[-2000:]}")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise on stdout; skip
                if message.get("id") == request_id and ("result" in message or "error" in message):
                    if "error" in message:
                        raise McpError(f"server error: {json.dumps(message['error'], ensure_ascii=False)}")
                    return message["result"]
                # anything else is a notification or a response to another id
        finally:
            selector.unregister(self._proc.stdout)
            selector.close()

    def _request(self, method: str, params: Dict[str, Any],
                 expect_result: bool = True, timeout: Optional[float] = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not expect_result:
            return None
        return self._read_response(request_id, timeout or self.timeout)

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    # -- MCP surface -------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = (result or {}).get("tools", [])
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if isinstance(result, dict) and result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
            raise McpError(f"tool '{name}' returned an error: {' '.join(texts)!r}")
        return result


def main() -> int:
    """CLI smoke run: start a server, print its tool list, optionally call one.

    Example:
        MEMORYWEB_DB=/tmp/demo.db python3 tests/mcp_min_client.py memoryweb domains
    """
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    command, tool = args[0], (args[1] if len(args) > 1 else None)
    tool_args = json.loads(args[2]) if len(args) > 2 else {}
    with McpStdioClient(command) as client:
        tools = client.list_tools()
        print(f"{len(tools)} tools:")
        for t in tools:
            print(f"  - {t.get('name')}")
        if tool:
            result = client.call_tool(tool, tool_args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
