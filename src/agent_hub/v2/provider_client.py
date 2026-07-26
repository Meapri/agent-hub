"""Bounded subprocess client for the v2 provider worker ABI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable, Mapping

from .errors import HubV2Error
from .egress_proxy import ProviderEgressProxy
from .provider_manifests import manifest_for

MAX_WORKER_RESPONSE_BYTES = 32 * 1024 * 1024


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
            process_factory is subprocess.Popen
            if enforce_egress is None
            else bool(enforce_egress)
        )
        self._lock = threading.Lock()
        self._active: subprocess.Popen[str] | None = None

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        request = {
            "id": "request",
            "method": method,
            "params": dict(params or {}),
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONINSPECT", "PYTHONSTARTUP"}
        }
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
                raise HubV2Error(
                    "provider_egress_unavailable",
                    "OS-level provider egress enforcement is unavailable.",
                    scope="provider",
                )
            profile = (
                '(version 1) (allow default) '
                '(deny network-outbound (remote tcp "*:*") (remote udp "*:*") '
                '(with message "agent-hub-egress-denied")) '
                '(allow network-outbound (remote tcp "localhost:*"))'
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
                cwd=str(Path.cwd()),
            )
        except OSError as exc:
            if proxy is not None:
                proxy.close()
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
            process.terminate()
            try:
                process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
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

    def cancel(self) -> bool:
        with self._lock:
            process = self._active
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        return True
