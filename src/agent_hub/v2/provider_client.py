"""Bounded subprocess client for the v2 provider worker ABI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Mapping

from .contracts import require_identifier
from .errors import HubV2Error
from .egress_proxy import ProviderEgressProxy
from .provider_manifests import manifest_for

MAX_WORKER_RESPONSE_BYTES = 32 * 1024 * 1024
_COMMON_ENVIRONMENT = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "AGENT_HUB_CONFIG_DIR",
        "AGENT_HUB_CACHE_DIR",
    }
)
_PROVIDER_ENVIRONMENT_PREFIXES = {
    "claude": ("CLAUDE_CODEX_", "CLAUDE_CONFIG_DIR"),
    "grok": ("GROK_CODEX_",),
    "gemini": ("GOOGLE_ANTIGRAVITY_",),
    "gpt": ("CODEX_HOME", "OPENAI_CODEX_"),
}


def _sandbox_path(path: str | Path, *, field: str = "path") -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HubV2Error(
            "provider_sandbox_invalid_path",
            "A provider sandbox path could not be resolved safely.",
            scope="provider",
            safe_details={"field": field},
        ) from exc


def _is_strict_descendant(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _sbpl_path(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


def _validate_sandbox_home(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    home = _sandbox_path(raw, field="HOME")
    if not raw.is_absolute() or len(home.parts) < 3:
        raise HubV2Error(
            "provider_sandbox_invalid_path",
            "The provider sandbox HOME is not safely scoped.",
            scope="provider",
        )
    return home


def _validate_writable_path(
    path: str | Path,
    *,
    home: Path,
    field: str,
    require_home_descendant: bool,
) -> Path:
    raw = Path(path).expanduser()
    candidate = _sandbox_path(raw, field=field)
    safely_scoped = (
        raw.is_absolute() and len(candidate.parts) >= 3 and candidate != Path(candidate.anchor)
    )
    if require_home_descendant:
        safely_scoped = safely_scoped and _is_strict_descendant(candidate, home)
    elif candidate == home or candidate in home.parents:
        safely_scoped = False
    if not safely_scoped:
        raise HubV2Error(
            "provider_sandbox_invalid_path",
            "A provider sandbox writable path is not safely scoped.",
            scope="provider",
            safe_details={"field": field},
        )
    return candidate


def _worker_environment(provider: str) -> dict[str, str]:
    prefixes = _PROVIDER_ENVIRONMENT_PREFIXES.get(provider, ())
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _COMMON_ENVIRONMENT or any(key.startswith(prefix) for prefix in prefixes)
    }
    environment.update(
        {
            "HOME": environment.get("HOME") or str(Path.home()),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if provider == "gpt" and not environment.get("OPENAI_CODEX_BIN"):
        codex = shutil.which("codex", path=environment.get("PATH"))
        if codex:
            # Resolve the user-local launcher before the sandbox starts. The
            # worker may read CODEX_HOME, but it must not scan unrelated home
            # directories merely to discover the executable.
            environment["OPENAI_CODEX_BIN"] = str(Path(codex).resolve(strict=False))
    return environment


def _sandbox_profile(
    *,
    provider: str,
    runtime_directory: str,
    environment: Mapping[str, str],
    proxy_port: int,
    python_executable: str | None = None,
) -> str:
    home = _validate_sandbox_home(environment.get("HOME") or Path.home())
    config_prefix = {
        "claude": ("CLAUDE_CODEX_CONFIG_DIR", home / ".config" / "claude-codex"),
        "grok": ("GROK_CODEX_CONFIG_DIR", home / ".config" / "grok-codex"),
        "gemini": (
            "GOOGLE_ANTIGRAVITY_CONFIG_DIR",
            home / ".config" / "google-antigravity-codex",
        ),
        "gpt": ("OPENAI_CODEX_CONFIG_DIR", home / ".config" / "openai-codex"),
    }
    cache_prefix = {
        "claude": ("CLAUDE_CODEX_CACHE_DIR", home / ".cache" / "claude-codex"),
        "grok": ("GROK_CODEX_CACHE_DIR", home / ".cache" / "grok-codex"),
        "gemini": (
            "GOOGLE_ANTIGRAVITY_CACHE_DIR",
            home / ".cache" / "google-antigravity-codex",
        ),
        "gpt": ("OPENAI_CODEX_CACHE_DIR", home / ".cache" / "openai-codex"),
    }
    writable = [
        _validate_writable_path(
            runtime_directory,
            home=home,
            field="runtime_directory",
            require_home_descendant=False,
        )
    ]
    for prefix, default in (config_prefix[provider], cache_prefix[provider]):
        writable.append(
            _validate_writable_path(
                environment.get(prefix) or default,
                home=home,
                field=prefix,
                require_home_descendant=True,
            )
        )
    if provider == "claude":
        writable.append(
            _validate_writable_path(
                home / ".claude",
                home=home,
                field="CLAUDE_HOME",
                require_home_descendant=True,
            )
        )
    if provider == "gpt":
        writable.append(
            _validate_writable_path(
                environment.get("CODEX_HOME") or home / ".codex",
                home=home,
                field="CODEX_HOME",
                require_home_descendant=True,
            )
        )
    writable = list(dict.fromkeys(_sandbox_path(path, field="writable_path") for path in writable))
    runtime_path = writable[0]
    writable_filters = " ".join(f"(subpath {_sbpl_path(path)})" for path in writable)
    interpreter = Path(python_executable or sys.executable).expanduser()
    runtime_roots = [
        *writable,
        _sandbox_path(sys.prefix),
        _sandbox_path(sys.base_prefix),
        _sandbox_path(sys.exec_prefix),
        _sandbox_path(sys.base_exec_prefix),
        _sandbox_path(Path(__file__).resolve().parents[2]),
        _sandbox_path(interpreter.parent.parent, field="python_executable"),
    ]
    readable_directories = [
        path for path in dict.fromkeys(runtime_roots) if _is_strict_descendant(path, home)
    ]
    readable_filters = " ".join(f"(subpath {_sbpl_path(path)})" for path in readable_directories)
    executable_candidates = [
        interpreter.absolute(),
        _sandbox_path(interpreter, field="python_executable"),
    ]
    if provider == "gpt" and environment.get("OPENAI_CODEX_BIN"):
        codex_binary = Path(str(environment["OPENAI_CODEX_BIN"])).expanduser()
        if codex_binary.is_absolute():
            executable_candidates.extend(
                (
                    codex_binary.absolute(),
                    _sandbox_path(codex_binary, field="OPENAI_CODEX_BIN"),
                )
            )
    executable_paths = [
        path for path in dict.fromkeys(executable_candidates) if _is_strict_descendant(path, home)
    ]
    literal_paths: list[Path] = [*executable_paths]
    readable_ancestors: list[Path] = []
    for candidate in (*readable_directories, *executable_paths):
        for parent in candidate.parents:
            if parent == home:
                break
            readable_ancestors.append(parent)
    literal_paths.extend(readable_ancestors)
    readable_literals = [
        home,
        home / ".config",
        home / ".cache",
        Path(environment.get("AGENT_HUB_CONFIG_DIR") or home / ".config" / "agent-hub"),
        Path(environment.get("AGENT_HUB_CONFIG_DIR") or home / ".config" / "agent-hub")
        / "settings.json",
    ]
    if provider == "claude":
        # Claude Code keeps non-secret account metadata beside its credential
        # directory and uses it to shape subscription OAuth requests.
        readable_literals.append(home / ".claude.json")
    literal_paths.extend(_sandbox_path(path) for path in readable_literals)
    readable_filters = " ".join(
        [
            readable_filters,
            *(
                f"(literal {_sbpl_path(path)})"
                for path in dict.fromkeys(literal_paths)
                if path == home or _is_strict_descendant(path, home)
            ),
        ]
    )
    return (
        "(version 1) (allow default) "
        f"(deny file-read* (require-all "
        f"(subpath {_sbpl_path(home)}) "
        f"(require-not (require-any {readable_filters})))) "
        f"(deny file-write* (require-not (require-any {writable_filters}))) "
        '(deny network-outbound (remote tcp "*:*") (remote udp "*:*") '
        '(with message "agent-hub-egress-denied")) '
        '(deny network-outbound (literal "/private/var/run/mDNSResponder") '
        '(with message "agent-hub-dns-denied")) '
        '(deny mach-lookup (global-name "com.apple.dnssd.service") '
        '(with message "agent-hub-dns-denied")) '
        f"(deny network-outbound (subpath "
        f"{_sbpl_path(home)}) "
        f"(subpath {_sbpl_path(runtime_path)}) "
        f"(subpath {_sbpl_path(runtime_path.parent)}) "
        '(subpath "/private/tmp") (subpath "/private/var/tmp") '
        '(with message "agent-hub-local-socket-denied")) '
        f'(allow network-outbound (remote tcp "localhost:{proxy_port}"))'
    )


class ProviderWorkerClient:
    def __init__(
        self,
        provider: str,
        *,
        python_executable: str | None = None,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        enforce_egress: bool | None = None,
    ) -> None:
        self.manifest = manifest_for(provider)
        self.provider = self.manifest["provider_id"]
        self.python_executable = python_executable or sys.executable
        self._process_factory = process_factory
        self._enforce_egress = (
            process_factory is subprocess.Popen if enforce_egress is None else bool(enforce_egress)
        )
        self._lock = threading.Lock()
        self._active: subprocess.Popen[str] | None = None

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        request_id: str = "request",
    ) -> dict[str, Any]:
        correlation_id = require_identifier(request_id, field="provider_request_id")
        request = {
            "id": correlation_id,
            "method": method,
            "params": dict(params or {}),
        }
        env = _worker_environment(self.provider)
        runtime_directory = tempfile.TemporaryDirectory(prefix=f"agent-hub-{self.provider}-worker-")
        env["TMPDIR"] = runtime_directory.name
        proxy = (
            ProviderEgressProxy(self.manifest["allowed_domains"]).start()
            if self._enforce_egress
            else None
        )
        if proxy is not None:
            env.update(
                {
                    "HTTP_PROXY": proxy.url,
                    "HTTPS_PROXY": proxy.url,
                    "ALL_PROXY": proxy.url,
                    "AGENT_HUB_EGRESS_PROXY": proxy.url,
                    "http_proxy": proxy.url,
                    "https_proxy": proxy.url,
                    "all_proxy": proxy.url,
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                }
            )
        worker_command = [
            self.python_executable,
            "-m",
            "agent_hub.v2.provider_worker",
            self.provider,
        ]
        if proxy is not None:
            sandbox = Path("/usr/bin/sandbox-exec")
            if sys.platform != "darwin" or not sandbox.exists():
                proxy.close()
                runtime_directory.cleanup()
                raise HubV2Error(
                    "provider_egress_unavailable",
                    "OS-level provider egress enforcement is unavailable.",
                    scope="provider",
                )
            try:
                profile = _sandbox_profile(
                    provider=self.provider,
                    runtime_directory=runtime_directory.name,
                    environment=env,
                    proxy_port=proxy.port,
                    python_executable=self.python_executable,
                )
            except Exception:
                proxy.close()
                runtime_directory.cleanup()
                raise
            worker_command = [str(sandbox), "-p", profile, *worker_command]
        try:
            process = self._process_factory(
                worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=runtime_directory.name,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            if proxy is not None:
                proxy.close()
            runtime_directory.cleanup()
            raise HubV2Error(
                "provider_worker_unavailable",
                "The provider worker could not be started.",
                scope="provider",
                retryable=True,
                safe_details={"provider": self.provider},
            ) from exc
        with self._lock:
            self._active = process
        try:
            stdout, _stderr = process.communicate(
                json.dumps(request, ensure_ascii=False) + "\n",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate_process(process, wait=True)
            raise HubV2Error(
                "provider_timeout",
                "The provider worker exceeded its time budget.",
                scope="provider",
                retryable=True,
                safe_details={"provider": self.provider},
            ) from exc
        finally:
            with self._lock:
                if self._active is process:
                    self._active = None
            if proxy is not None:
                proxy.close()
            runtime_directory.cleanup()
        if process.returncode != 0:
            raise HubV2Error(
                "provider_worker_failed",
                "The provider worker stopped unexpectedly.",
                scope="provider",
                retryable=True,
                safe_details={"provider": self.provider, "returncode": process.returncode},
            )
        if len(stdout.encode("utf-8")) > MAX_WORKER_RESPONSE_BYTES:
            raise HubV2Error(
                "provider_response_too_large",
                "The provider worker response exceeded the local limit.",
                scope="provider",
            )
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise HubV2Error(
                "provider_protocol_error",
                "The provider worker returned an invalid response.",
                scope="provider",
                retryable=True,
            )
        try:
            parsed = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise HubV2Error(
                "provider_protocol_error",
                "The provider worker returned invalid JSON.",
                scope="provider",
                retryable=True,
            ) from exc
        if not isinstance(parsed, dict):
            raise HubV2Error(
                "provider_protocol_error",
                "The provider worker response must be an object.",
                scope="provider",
            )
        if parsed.get("id") != correlation_id:
            raise HubV2Error(
                "provider_protocol_error",
                "The provider worker response correlation ID does not match.",
                scope="provider",
                retryable=True,
            )
        if parsed.get("success") is not True:
            error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            raise HubV2Error(
                str(error.get("code") or "provider_request_failed"),
                str(error.get("message") or "The provider request failed."),
                scope=str(error.get("scope") or "provider"),
                retryable=bool(error.get("retryable", False)),
                safe_details=error.get("safe_details")
                if isinstance(error.get("safe_details"), dict)
                else {},
            )
        result = parsed.get("result")
        if not isinstance(result, dict):
            raise HubV2Error(
                "provider_protocol_error",
                "The provider result must be an object.",
                scope="provider",
            )
        return result

    @staticmethod
    def _signal_process(process: subprocess.Popen[str], signal_number: int) -> None:
        process_id = getattr(process, "pid", None)
        if os.name == "posix" and isinstance(process_id, int) and process_id > 0:
            try:
                os.killpg(process_id, signal_number)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        try:
            if signal_number == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except OSError:
            pass

    @classmethod
    def _terminate_process(
        cls,
        process: subprocess.Popen[str],
        *,
        wait: bool,
    ) -> None:
        cls._signal_process(process, signal.SIGTERM)
        if not wait:
            return
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            cls._signal_process(process, signal.SIGKILL)
            process.communicate()

    def cancel(self) -> bool:
        with self._lock:
            process = self._active
        if process is None or process.poll() is not None:
            return False
        self._terminate_process(process, wait=False)
        return True
