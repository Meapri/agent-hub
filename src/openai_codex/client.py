"""Bounded subprocess transports for the official Codex CLI.

Agent Hub never opens Codex credential files.  Status/model requests use the
documented app-server JSONL protocol, while generation uses an ephemeral
``codex exec`` process with user config, MCPs, rules, shell, and web search
disabled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
from threading import Thread
import time
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from agent_hub.core import limits

from agent_hub import __version__ as AGENT_HUB_VERSION

from .errors import (
    CodexProcessError,
    CodexProtocolError,
    CodexSideEffectRefused,
    CodexTimeout,
    CodexUnavailable,
)

MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 512 * 1024
MAX_EVENT_COUNT = 10_000
MAX_PROMPT_CHARS = 2_000_000
SIDE_EFFECT_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "collab_tool_call",
    "web_search",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r'(?i)("?(?:access|refresh|id)_token"?\s*[:=]\s*")[^"]+(")'),
)


def redact(text: Any, *, limit: int = 2_000) -> str:
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            value = pattern.sub(r"\1[redacted]\2", value)
        else:
            value = pattern.sub("[redacted]", value)
    return value[:limit]


def codex_binary() -> str:
    configured = os.getenv("OPENAI_CODEX_BIN", "").strip()
    candidate = shutil.which(configured or "codex")
    if not candidate and configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            candidate = str(path)
    if not candidate:
        raise CodexUnavailable(
            "Official Codex CLI was not found. Install Codex and ensure `codex` is on PATH, "
            "or set OPENAI_CODEX_BIN to its executable path."
        )
    return candidate


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass


def _safe_env() -> Dict[str, str]:
    env = dict(os.environ)
    for key in (
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "RUST_LOG",
        "LOG_FORMAT",
    ):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    return env


def run_bounded(
    argv: Sequence[str],
    *,
    input_text: str,
    timeout: float,
    cwd: str | None = None,
) -> Tuple[str, str, int]:
    """Run a fixed argv without a shell and bound time plus both output streams."""

    if len(input_text) > MAX_PROMPT_CHARS:
        raise ValueError(f"Codex input exceeds {MAX_PROMPT_CHARS} characters")
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=_safe_env(),
            shell=False,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CodexUnavailable(f"Could not start official Codex CLI: {redact(exc)}") from exc

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    write_error: List[Exception] = []

    def write_stdin() -> None:
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            write_error.append(exc)

    writer = Thread(target=write_stdin, daemon=True)
    writer.start()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    chunks: Dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": MAX_STDOUT_BYTES, "stderr": MAX_STDERR_BYTES}
    deadline = time.monotonic() + max(0.1, float(timeout))

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexTimeout(f"Codex process exceeded {float(timeout):g} seconds")
            events = selector.select(timeout=min(remaining, 0.25))
            if not events and proc.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _mask in events:
                try:
                    data = os.read(key.fd, 65_536)
                except OSError:
                    data = b""
                if not data:
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    continue
                target = chunks[str(key.data)]
                target.extend(data)
                if len(target) > limits[str(key.data)]:
                    raise CodexProcessError(
                        f"Codex {key.data} exceeded the bounded output limit"
                    )
        remaining = max(0.01, deadline - time.monotonic())
        returncode = proc.wait(timeout=remaining)
    except (CodexTimeout, CodexProcessError):
        _terminate_process(proc)
        raise
    except subprocess.TimeoutExpired as exc:
        _terminate_process(proc)
        raise CodexTimeout(f"Codex process exceeded {float(timeout):g} seconds") from exc
    finally:
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    stdout = chunks["stdout"].decode("utf-8", errors="replace")
    stderr = chunks["stderr"].decode("utf-8", errors="replace")
    if write_error and not stdout:
        raise CodexProcessError("Codex closed stdin before accepting the request")
    return stdout, stderr, returncode


def _json_lines(stdout: str) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        if len(messages) >= MAX_EVENT_COUNT:
            raise CodexProtocolError("Codex emitted too many JSONL messages")
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise CodexProtocolError("Codex emitted invalid JSONL output") from exc
        if not isinstance(value, dict):
            raise CodexProtocolError("Codex JSONL messages must be objects")
        messages.append(value)
    return messages


def _app_server_messages(
    payload: str,
    *,
    expected_ids: set[int],
    timeout: float,
) -> List[Dict[str, Any]]:
    """Keep stdin open until app-server has answered every requested id."""

    try:
        proc = subprocess.Popen(
            [codex_binary(), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_safe_env(),
            shell=False,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CodexUnavailable(f"Could not start official Codex CLI: {redact(exc)}") from exc
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    pending = bytearray()
    messages: List[Dict[str, Any]] = []
    received_ids: set[int] = set()
    deadline = time.monotonic() + max(0.1, float(timeout))
    try:
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.flush()
        while not expected_ids.issubset(received_ids):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexTimeout(
                    f"Codex app-server exceeded {float(timeout):g} seconds"
                )
            events = selector.select(timeout=min(remaining, 0.25))
            if not events and proc.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _mask in events:
                try:
                    data = os.read(key.fd, 65_536)
                except OSError:
                    data = b""
                if not data:
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    continue
                if key.data == "stderr":
                    stderr.extend(data)
                    if len(stderr) > MAX_STDERR_BYTES:
                        raise CodexProcessError(
                            "Codex app-server stderr exceeded the bounded output limit"
                        )
                    continue
                stdout.extend(data)
                pending.extend(data)
                if len(stdout) > MAX_STDOUT_BYTES:
                    raise CodexProcessError(
                        "Codex app-server stdout exceeded the bounded output limit"
                    )
                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if not raw_line.strip():
                        continue
                    if len(messages) >= MAX_EVENT_COUNT:
                        raise CodexProtocolError(
                            "Codex app-server emitted too many JSONL messages"
                        )
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise CodexProtocolError(
                            "Codex app-server emitted invalid JSONL output"
                        ) from exc
                    if not isinstance(value, dict):
                        raise CodexProtocolError(
                            "Codex app-server JSONL messages must be objects"
                        )
                    messages.append(value)
                    if isinstance(value.get("id"), int):
                        received_ids.add(value["id"])
            if proc.poll() is not None and not selector.get_map():
                break
        if not expected_ids.issubset(received_ids):
            raise CodexProtocolError(
                "Codex app-server exited before all responses arrived: "
                f"{redact(stderr.decode('utf-8', errors='replace')) or 'no details'}"
            )
        return messages
    finally:
        selector.close()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def app_server_batch(
    requests: Iterable[Tuple[str, Dict[str, Any]]],
    *,
    timeout: float = 20.0,
) -> List[Dict[str, Any]]:
    """Execute stable, non-streaming app-server requests in one handshake."""

    lines: List[Dict[str, Any]] = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "agent_hub",
                    "title": "Agent Hub",
                    "version": AGENT_HUB_VERSION,
                }
            },
        },
        {"method": "initialized", "params": {}},
    ]
    expected: List[int] = []
    for index, (method, params) in enumerate(requests, start=2):
        expected.append(index)
        lines.append({"id": index, "method": method, "params": params})
    payload = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in lines)
    messages = _app_server_messages(
        payload,
        expected_ids={1, *expected},
        timeout=timeout,
    )
    by_id = {item.get("id"): item for item in messages if "id" in item}
    initialize = by_id.get(1)
    if not isinstance(initialize, dict) or "result" not in initialize:
        raise CodexProtocolError("Codex app-server did not complete initialization")
    results: List[Dict[str, Any]] = []
    for request_id in expected:
        message = by_id.get(request_id)
        if not isinstance(message, dict):
            raise CodexProtocolError(f"Codex app-server omitted response id {request_id}")
        if isinstance(message.get("error"), dict):
            error = message["error"]
            raise CodexProtocolError(
                f"Codex app-server request failed: {redact(error.get('message'))}"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexProtocolError("Codex app-server result must be an object")
        results.append(result)
    return results


def app_server_request(
    method: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    return app_server_batch([(method, params or {})], timeout=timeout)[0]


def exec_argv(
    *,
    cwd: str,
    model: str = "",
    reasoning_effort: str = "",
    image_paths: Iterable[str] = (),
) -> List[str]:
    """Build a prompt-free argv suitable for audit and unit testing."""

    argv = [
        codex_binary(),
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--disable",
        "shell_tool",
        "-c",
        'web_search="disabled"',
        "-c",
        "tools.update_plan.enabled=false",
        "-c",
        "tools.experimental_request_user_input.enabled=false",
        "-C",
        cwd,
    ]
    if model:
        argv.extend(["--model", model])
    if reasoning_effort:
        argv.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    for path in image_paths:
        argv.extend(["--image", path])
    argv.append("-")
    return argv


def run_exec_chat(
    prompt: str,
    *,
    cwd: str,
    model: str = "",
    reasoning_effort: str = "",
    image_paths: Iterable[str] = (),
    timeout: float = limits.MAX_PROVIDER_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    stdout, stderr, returncode = run_bounded(
        exec_argv(
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            image_paths=image_paths,
        ),
        input_text=prompt,
        timeout=timeout,
        cwd=cwd,
    )
    events = _json_lines(stdout)
    final_text = ""
    usage: Dict[str, Any] = {}
    completed = False
    failure = ""
    for event in events:
        event_type = str(event.get("type") or "")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        if event_type in {"item.started", "item.updated", "item.completed"}:
            if item_type in SIDE_EFFECT_ITEM_TYPES:
                raise CodexSideEffectRefused(
                    f"Codex attempted disallowed side effect item: {item_type}"
                )
            if event_type == "item.completed" and item_type == "agent_message":
                final_text = str(item.get("text") or "").strip()
            if item_type == "error" and item.get("message"):
                failure = redact(item.get("message"))
        elif event_type == "turn.completed":
            completed = True
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = {
                    "prompt_tokens": raw_usage.get("input_tokens"),
                    "cached_prompt_tokens": raw_usage.get("cached_input_tokens"),
                    "completion_tokens": raw_usage.get("output_tokens"),
                    "reasoning_tokens": raw_usage.get("reasoning_output_tokens"),
                }
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
                    usage["total_tokens"] = prompt_tokens + completion_tokens
        elif event_type == "turn.failed":
            error = event.get("error")
            failure = redact(error.get("message") if isinstance(error, dict) else error)
        elif event_type == "error":
            failure = redact(event.get("message"))

    if returncode != 0 or failure:
        raise CodexProcessError(
            failure
            or f"Codex exec exited with {returncode}: {redact(stderr) or 'no details'}"
        )
    if not completed:
        raise CodexProtocolError("Codex exec ended without turn.completed")
    if not final_text:
        raise CodexProtocolError("Codex exec completed without a final agent message")
    return {"text": final_text, "usage": usage, "event_count": len(events)}
