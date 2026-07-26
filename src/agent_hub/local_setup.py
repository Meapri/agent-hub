"""Deterministic, opt-in rendering of machine-local Agent Hub MCP config."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Dict, Sequence

MAX_CONFIG_BYTES = 1024 * 1024
MANAGED_TOML_BEGIN = "# BEGIN AGENT HUB LOCAL MCP"
MANAGED_TOML_END = "# END AGENT HUB LOCAL MCP"
CONFIG_PATHS = (
    ".codex/config.toml",
    ".cursor/mcp.json",
    ".gemini/settings.json",
    ".mcp.json",
    "hubs/codex/.mcp.json",
    "hubs/claude-code/.mcp.json",
)
_LEGACY_TOML_SECTIONS = {
    "mcp_servers.memory",
    "mcp_servers.memory.env",
    "mcp_servers.agent-hub",
}
_TOML_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


class SetupError(RuntimeError):
    """A local config could not be safely planned or applied."""


@dataclass(frozen=True)
class ConfigChange:
    relative_path: str
    target: Path
    status: str
    expected_sha256: str | None
    rendered_sha256: str
    rendered: bytes

    def public(self) -> Dict[str, Any]:
        return {
            "path": self.relative_path,
            "status": self.status,
            "before_sha256": self.expected_sha256,
            "after_sha256": self.rendered_sha256,
        }


@dataclass(frozen=True)
class SetupPlan:
    repo_root: Path
    target_root: Path
    changes: tuple[ConfigChange, ...]

    @property
    def changed(self) -> tuple[ConfigChange, ...]:
        return tuple(item for item in self.changes if item.status != "unchanged")

    def public(self) -> Dict[str, Any]:
        return {
            "schema": "agent_hub_local_setup_plan_v1",
            "repo_root": str(self.repo_root),
            "target_root": str(self.target_root),
            "apply_required": bool(self.changed),
            "changed": len(self.changed),
            "files": [item.public() for item in self.changes],
            "actions_excluded": [
                "dependency installation",
                "provider login or logout",
                "global plugin registration",
                "network access",
            ],
        }


def _canonical_directory(value: str | os.PathLike[str], *, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise SetupError(f"{label} does not exist: {raw}") from exc
    if not resolved.is_dir():
        raise SetupError(f"{label} is not a directory: {resolved}")
    return resolved


def _digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _validate_target_parent(target_root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(target_root)
    except ValueError as exc:
        raise SetupError(f"config target escapes target_root: {path}") from exc
    current = target_root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise SetupError(f"refusing unsafe config parent: {current}")


def _read_optional(path: Path, *, target_root: Path) -> bytes | None:
    _validate_target_parent(target_root, path)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_stat.st_mode):
        raise SetupError(f"refusing symlinked config target: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise SetupError(f"config target is not a regular file: {path}")
    if path_stat.st_nlink != 1:
        raise SetupError(f"refusing hard-linked config target: {path}")
    current_uid = getattr(os, "getuid", lambda: path_stat.st_uid)()
    if path_stat.st_uid != current_uid:
        raise SetupError(f"config target is not owned by the current user: {path}")
    if path_stat.st_size > MAX_CONFIG_BYTES:
        raise SetupError(f"config target is too large: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SetupError(f"could not read config target: {path}") from exc


def _json_object(content: bytes | None, *, path: Path) -> Dict[str, Any]:
    if content is None or not content.strip():
        return {}
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"config is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(parsed, dict):
        raise SetupError(f"config root must be a JSON object: {path}")
    return parsed


def _is_legacy_memory_server(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    args = value.get("args")
    env = value.get("env")
    return (
        value.get("command") == "uvx"
        and isinstance(args, list)
        and args[:2] == ["basic-memory", "mcp"]
        and isinstance(env, dict)
        and "BASIC_MEMORY_HOME" in env
    )


def _hub_server(
    repo_root: Path,
    *,
    include_type: bool,
    hub_executable: str = "agent-hub-mcp",
) -> Dict[str, Any]:
    server: Dict[str, Any] = {
        "command": str(repo_root / ".venv" / "bin" / hub_executable),
    }
    if include_type:
        server["type"] = "stdio"
    return server


def _merge_json_config(
    existing: bytes | None,
    *,
    path: Path,
    repo_root: Path,
    wrapper: bool,
    include_type: bool,
    gemini: bool = False,
    hub_executable: str = "agent-hub-mcp",
) -> bytes:
    root = _json_object(existing, path=path)
    if gemini:
        root["contextFileName"] = "AGENTS.md"
    if wrapper:
        raw_servers = root.get("mcpServers")
        if raw_servers is None:
            servers: Dict[str, Any] = {}
        elif isinstance(raw_servers, dict):
            servers = dict(raw_servers)
        else:
            raise SetupError(f"mcpServers must be a JSON object: {path}")
        root["mcpServers"] = servers
    else:
        servers = root
    if _is_legacy_memory_server(servers.get("memory")):
        servers.pop("memory")
    servers["agent-hub"] = _hub_server(
        repo_root,
        include_type=include_type,
        hub_executable=hub_executable,
    )
    return (json.dumps(root, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _strip_managed_toml(text: str) -> str:
    begin_count = text.count(MANAGED_TOML_BEGIN)
    end_count = text.count(MANAGED_TOML_END)
    if begin_count or end_count:
        if begin_count != 1 or end_count != 1:
            raise SetupError("Codex config has malformed Agent Hub managed markers")
        before, remainder = text.split(MANAGED_TOML_BEGIN, 1)
        managed, after = remainder.split(MANAGED_TOML_END, 1)
        # Older generated files could contain user-owned top-level settings
        # before the first managed MCP table. Keep those settings while
        # removing only the tables Agent Hub owns.
        text = "\n".join(
            part for part in (before.rstrip(), managed.strip(), after.lstrip()) if part
        )

    kept: list[str] = []
    dropping = False
    for line in text.splitlines():
        match = _TOML_SECTION_RE.match(line)
        if match:
            dropping = match.group(1).strip() in _LEGACY_TOML_SECTIONS
        if not dropping:
            kept.append(line)
    return "\n".join(kept).strip()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_codex_config(
    existing: bytes | None,
    *,
    path: Path,
    repo_root: Path,
    hub_executable: str = "agent-hub-mcp",
) -> bytes:
    if existing is None:
        text = ""
    else:
        try:
            text = existing.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SetupError(f"config is not valid UTF-8 TOML: {path}") from exc
    prefix = _strip_managed_toml(text)
    hub_command = repo_root / ".venv" / "bin" / hub_executable
    block = "\n".join(
        (
            MANAGED_TOML_BEGIN,
            "[mcp_servers.agent-hub]",
            f"command = {_toml_string(str(hub_command))}",
            'type = "stdio"',
            MANAGED_TOML_END,
        )
    )
    rendered = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"
    return rendered.encode("utf-8")


def _render_one(
    relative_path: str,
    *,
    existing: bytes | None,
    target: Path,
    repo_root: Path,
    hub_executable: str = "agent-hub-mcp",
) -> bytes:
    if relative_path == ".codex/config.toml":
        return _render_codex_config(
            existing,
            path=target,
            repo_root=repo_root,
            hub_executable=hub_executable,
        )
    if relative_path == ".cursor/mcp.json":
        return _merge_json_config(
            existing,
            path=target,
            repo_root=repo_root,
            wrapper=True,
            include_type=True,
            hub_executable=hub_executable,
        )
    if relative_path == ".gemini/settings.json":
        return _merge_json_config(
            existing,
            path=target,
            repo_root=repo_root,
            wrapper=True,
            include_type=False,
            gemini=True,
            hub_executable=hub_executable,
        )
    if relative_path == ".mcp.json":
        return _merge_json_config(
            existing,
            path=target,
            repo_root=repo_root,
            wrapper=True,
            include_type=True,
            hub_executable=hub_executable,
        )
    if relative_path == "hubs/claude-code/.mcp.json":
        return _merge_json_config(
            existing,
            path=target,
            repo_root=repo_root,
            wrapper=True,
            include_type=False,
            hub_executable=hub_executable,
        )
    if relative_path == "hubs/codex/.mcp.json":
        return _merge_json_config(
            existing,
            path=target,
            repo_root=repo_root,
            wrapper=False,
            include_type=False,
            hub_executable=hub_executable,
        )
    raise SetupError(f"unsupported local config target: {relative_path}")


def plan_setup(
    repo_root: str | os.PathLike[str],
    *,
    target_root: str | os.PathLike[str] | None = None,
    hub_executable: str = "agent-hub-mcp",
) -> SetupPlan:
    if hub_executable != "agent-hub-mcp":
        raise SetupError("unsupported Agent Hub MCP executable")
    source = _canonical_directory(repo_root, label="repo_root")
    destination = _canonical_directory(
        target_root if target_root is not None else source,
        label="target_root",
    )
    changes = []
    for relative_path in CONFIG_PATHS:
        target = destination / relative_path
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise SetupError(f"config target escapes target_root: {target}") from exc
        existing = _read_optional(target, target_root=destination)
        rendered = _render_one(
            relative_path,
            existing=existing,
            target=target,
            repo_root=source,
            hub_executable=hub_executable,
        )
        expected_sha = _digest(existing) if existing is not None else None
        status = "create" if existing is None else "unchanged" if existing == rendered else "update"
        changes.append(
            ConfigChange(
                relative_path=relative_path,
                target=target,
                status=status,
                expected_sha256=expected_sha,
                rendered_sha256=_digest(rendered),
                rendered=rendered,
            )
        )
    return SetupPlan(
        repo_root=source,
        target_root=destination,
        changes=tuple(changes),
    )


def _current_digest(path: Path, *, target_root: Path) -> str | None:
    content = _read_optional(path, target_root=target_root)
    return _digest(content) if content is not None else None


def _atomic_write(path: Path, content: bytes, *, target_root: Path) -> None:
    _validate_target_parent(target_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise SetupError(f"refusing non-regular config target: {path}")
        mode = stat.S_IMODE(current.st_mode)
    else:
        mode = 0o600
    descriptor: int | None = None
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    except OSError as exc:
        raise SetupError(f"could not atomically write config: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def apply_plan(plan: SetupPlan) -> Dict[str, Any]:
    """Apply a previously inspected plan with an all-file CAS preflight."""

    changed = plan.changed
    for item in changed:
        if _current_digest(item.target, target_root=plan.target_root) != item.expected_sha256:
            raise SetupError(f"config changed after planning; rerun setup: {item.relative_path}")
    for item in changed:
        _atomic_write(
            item.target,
            item.rendered,
            target_root=plan.target_root,
        )
    result = plan.public()
    result.update(
        {
            "schema": "agent_hub_local_setup_result_v1",
            "applied": len(changed),
            "success": True,
        }
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply machine-local Agent Hub MCP paths. The default is a read-only dry run."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[2]),
        help="Agent Hub checkout whose executable and memory paths should be used.",
    )
    parser.add_argument(
        "--target-root",
        help="Config tree to render into; defaults to --repo-root.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Write the reviewed changes atomically.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Return exit 1 when generated config differs.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--quiet", action="store_true", help="Suppress unchanged text output.")
    return parser


def _text_report(plan: SetupPlan, *, applied: bool, quiet: bool) -> str:
    lines = []
    for item in plan.changes:
        if quiet and item.status == "unchanged":
            continue
        lines.append(f"{item.status:9} {item.relative_path}")
    if applied:
        lines.append(f"applied {len(plan.changed)} local config change(s)")
    elif plan.changed:
        lines.append(f"dry-run: {len(plan.changed)} change(s); rerun with --apply to write them")
    elif not quiet:
        lines.append("local config is up to date")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = plan_setup(
            args.repo_root,
            target_root=args.target_root,
        )
        if args.apply:
            payload = apply_plan(plan)
        else:
            payload = plan.public()
            payload["success"] = not (args.check and plan.changed)
            payload["applied"] = 0
    except SetupError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": "agent_hub_local_setup_error_v1",
                        "success": False,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"error: {exc}", file=os.sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        report = _text_report(plan, applied=args.apply, quiet=args.quiet)
        if report:
            print(report)
    return 1 if args.check and plan.changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
