"""Minimal MCP stdio client used by the autonomous broker.

An MCP server cannot call sibling MCP servers — only the host can. So to run a
recipe end-to-end the broker must itself become an MCP *client*: it spawns each
leaf's stdio server as a subprocess and speaks line-delimited JSON-RPC to it.
Each leaf process keeps enforcing its own consent/auth, so the broker automates
routing without being able to bypass a leaf's own gating.

POSIX-oriented (select on pipes); the target platform is macOS/Linux.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PROTOCOL_VERSION = "2024-11-05"


class LeafError(Exception):
    """Transport/spawn level failure (distinct from a tool-level error result)."""


class LeafClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[List[str]] = None,
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        default_timeout: float = 120.0,
    ) -> None:
        self.name = name
        self._default_timeout = default_timeout
        self._id = 0
        self._stderr_tail: List[str] = []
        full_env = dict(os.environ)
        full_env.update({str(k): str(v) for k, v in (env or {}).items()})
        try:
            self._proc = subprocess.Popen(
                [command, *(args or [])],
                cwd=cwd or None,
                env=full_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise LeafError(f"{name}: cannot spawn leaf ({command}): {exc}") from exc
        # Drain stderr so a chatty leaf can't deadlock on a full pipe.
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._initialize()

    # -- lifecycle -----------------------------------------------------------
    def _drain_stderr(self) -> None:
        try:
            assert self._proc.stderr is not None
            for line in self._proc.stderr:
                self._stderr_tail.append(line.rstrip())
                del self._stderr_tail[:-20]  # keep only the last ~20 lines
        except (OSError, ValueError):
            pass

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "orchestrate-codex-broker", "version": "0.4.0"},
            },
            timeout=min(self._default_timeout, 30.0),
        )
        self._notify("notifications/initialized")

    def close(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except OSError:
            pass

    def __enter__(self) -> "LeafClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- JSON-RPC ------------------------------------------------------------
    def _write(self, message: Dict[str, Any]) -> None:
        if self._proc.stdin is None or self._proc.poll() is not None:
            raise LeafError(f"{self.name}: leaf not running")
        try:
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise LeafError(f"{self.name}: write failed: {exc}") from exc

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def _request(self, method: str, params: Optional[Dict[str, Any]], *, timeout: float) -> Dict[str, Any]:
        self._id += 1
        want = self._id
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": want, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        return self._read_response(want, timeout)

    def _read_response(self, want_id: int, timeout: float) -> Dict[str, Any]:
        assert self._proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeafError(f"{self.name}: timed out waiting for response")
            if self._proc.poll() is not None:
                tail = "; ".join(self._stderr_tail[-3:])
                raise LeafError(f"{self.name}: leaf exited (code {self._proc.returncode}). {tail}")
            ready, _, _ = select.select([self._proc.stdout], [], [], min(remaining, 1.0))
            if not ready:
                continue
            line = self._proc.stdout.readline()
            if line == "":
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate non-JSON noise on stdout
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
            # otherwise a notification or unrelated message — keep reading

    # -- high level ----------------------------------------------------------
    def list_tools(self, timeout: Optional[float] = None) -> List[str]:
        resp = self._request("tools/list", {}, timeout=timeout or self._default_timeout)
        result = resp.get("result") or {}
        return [str(t.get("name")) for t in result.get("tools", []) if t.get("name")]

    def call_tool(
        self, tool: str, arguments: Dict[str, Any], *, timeout: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Return (ok, text). ok=False means a tool-level error (not transport)."""
        resp = self._request(
            "tools/call",
            {"name": tool, "arguments": arguments or {}},
            timeout=timeout or self._default_timeout,
        )
        if "error" in resp:
            err = resp["error"] or {}
            return False, str(err.get("message") or err)
        result = resp.get("result")
        return _interpret_result(result)


def _interpret_result(result: Any) -> Tuple[bool, str]:
    if not isinstance(result, dict):
        return True, str(result or "")
    text = _extract_text(result)
    is_error = bool(result.get("isError")) or result.get("success") is False
    return (not is_error), text


def _extract_text(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(p for p in parts if p)
        if joined:
            return joined
    if isinstance(result.get("text"), str):
        return result["text"]
    return json.dumps(result, ensure_ascii=False)
