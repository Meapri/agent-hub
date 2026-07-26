"""Bounded subprocess client for the v2 provider worker ABI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
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
    return environment


def _sandbox_profile(
    *,
    provider: str,
    runtime_directory: str,
    environment: Mapping[str, str],
    proxy_port: int,
) -> str:
    home = Path(environment.get("HOME") or Path.home())
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
    writable = [Path(runtime_directory)]
    for prefix, default in (config_prefix[provider], cache_prefix[provider]):
        writable.append(Path(environment.get(prefix) or default).expanduser())
    if provider == "claude":
        writable.append(home / ".claude")
    if provider == "gpt":
        writable.append(Path(environment.get("CODEX_HOME") or home / ".codex").expanduser())
    writable_filters = " ".join(
        f"(subpath {json.dumps(str(path.resolve(strict=False)))})"
        for path in dict.fromkeys(writable)
    )
    readable_directories = [
        *writable,
        Path(sys.prefix),
        Path(sys.executable).resolve(strict=False).parents[3],
        Path(__file__).resolve().parents[3],
    ]
    readable_filters = " ".join(
        f"(subpath {json.dumps(str(path.resolve(strict=False)))})"
        for path in dict.fromkeys(readable_directories)
    )
    readable_filters += " " + " ".join(
        f"(literal {json.dumps(str(path))})"
        for path in dict.fromkeys(
            (
                Path(sys.executable),
                Path(sys.executable).resolve(strict=False),
            )
        )
    )
    readable_ancestors: list[Path] = []
    for path in (
        *readable_directories,
        Path(sys.executable),
        Path(sys.executable).resolve(strict=False),
    ):
        candidate = path.expanduser().absolute()
        if candidate != home and home not in candidate.parents:
            continue
        for parent in candidate.parents:
            if parent == home:
                break
            readable_ancestors.append(parent)
    readable_filters += " " + " ".join(
        f"(literal {json.dumps(str(path))})" for path in dict.fromkeys(readable_ancestors)
    )
    readable_filters += " " + " ".join(
        f"(literal {json.dumps(str(path.resolve(strict=False)))})"
        for path in (home, home / ".config", home / ".cache")
    )
    return (
        "(version 1) (allow default) "
        f"(deny file-read* (require-all "
        f"(subpath {json.dumps(str(home.resolve(strict=False)))}) "
        f"(require-not (require-any {readable_filters})))) "
        f"(deny file-write* (require-not (require-any {writable_filters}))) "
        '(deny network-outbound (remote tcp "*:*") (remote udp "*:*") '
        '(with message "agent-hub-egress-denied")) '
        '(deny network-outbound (literal "/private/var/run/mDNSResponder") '
        '(with message "agent-hub-dns-denied")) '
        '(deny mach-lookup (global-name "com.apple.dnssd.service") '
        '(with message "agent-hub-dns-denied")) '
        f"(deny network-outbound (subpath "
        f"{json.dumps(str(home.resolve(strict=False)))}) "
        f"(subpath {json.dumps(str(Path(runtime_directory).resolve(strict=False)))}) "
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
            profile = _sandbox_profile(
                provider=self.provider,
                runtime_directory=runtime_directory.name,
                environment=env,
                proxy_port=proxy.port,
            )
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
