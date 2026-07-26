"""Project-scoped v2 policy with digest-fenced prepare/apply updates."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from typing import Any, Mapping

from .contracts import ROUTING_MODES, canonical_project_root, require_non_negative_int
from .errors import HubV2Error
from .experimental import normalize_experimental_flags

POLICY_SCHEMA = "agent_hub_project_policy_v2"
POLICY_RELATIVE_PATH = Path(".agent-hub/project.toml")
MAX_POLICY_BYTES = 256 * 1024

DEFAULT_POLICY: dict[str, Any] = {
    "schema": POLICY_SCHEMA,
    "revision": 0,
    "routing_profile": "quality_balanced",
    "routing_mode": "shadow",
    "provider_allowlist": ["claude", "grok", "gemini", "gpt"],
    "model_allowlist": [],
    "egress": {
        "repository_content": "approval_required",
        "artifact_content": "approval_required",
        "inline_prompt": "allowed",
    },
    "budgets": {
        "timeout_seconds": 1790,
        "max_leaf_calls": 100,
        "max_tokens": 131072,
    },
    "artifact_retention": "durable_private",
    "workflow_locks": {},
    "plugin_locks": {},
    "experimental": {
        "isolated_tool_worker": False,
        "local_model": False,
        "remote_worker": False,
    },
}


@dataclass(frozen=True)
class PolicySnapshot:
    project_root: str
    path: str
    exists: bool
    file_sha256: str | None
    policy: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "schema": "agent_hub_policy_snapshot_v2",
            "project_root": self.project_root,
            "path": self.path,
            "exists": self.exists,
            "file_sha256": self.file_sha256,
            "policy": self.policy,
        }


def _policy_path(project_root: str) -> Path:
    return Path(canonical_project_root(project_root)) / POLICY_RELATIVE_PATH


def _safe_existing_file(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HubV2Error(
            "unsafe_policy_path",
            "The project policy target is not a safe regular file.",
            scope="policy",
        )
    if info.st_size > MAX_POLICY_BYTES:
        raise HubV2Error(
            "policy_too_large",
            "The project policy exceeds the size limit.",
            scope="policy",
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HubV2Error(
            "policy_read_failed",
            "The project policy could not be read.",
            scope="policy",
        ) from exc


def _normalize_policy(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("schema", POLICY_SCHEMA) != POLICY_SCHEMA:
        raise HubV2Error(
            "unsupported_policy_schema",
            "The project policy schema is not supported.",
            scope="policy",
        )
    revision = require_non_negative_int(raw.get("revision", 0), field="policy.revision")
    routing_mode = str(raw.get("routing_mode") or "shadow")
    if routing_mode not in ROUTING_MODES:
        raise HubV2Error(
            "invalid_policy",
            "The routing mode is not supported.",
            scope="policy",
        )
    providers = raw.get("provider_allowlist", DEFAULT_POLICY["provider_allowlist"])
    models = raw.get("model_allowlist", [])
    if not isinstance(providers, list) or not all(isinstance(item, str) for item in providers):
        raise HubV2Error(
            "invalid_policy",
            "provider_allowlist must be an array of strings.",
            scope="policy",
        )
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
        raise HubV2Error(
            "invalid_policy",
            "model_allowlist must be an array of strings.",
            scope="policy",
        )
    egress = raw.get("egress", {})
    budgets = raw.get("budgets", {})
    workflow_locks = raw.get("workflow_locks", {})
    plugin_locks = raw.get("plugin_locks", {})
    experimental = raw.get("experimental", {})
    if not all(
        isinstance(value, Mapping)
        for value in (egress, budgets, workflow_locks, plugin_locks, experimental)
    ):
        raise HubV2Error(
            "invalid_policy",
            "Policy sections must be objects.",
            scope="policy",
        )
    normalized = {
        "schema": POLICY_SCHEMA,
        "revision": revision,
        "routing_profile": str(raw.get("routing_profile") or "quality_balanced"),
        "routing_mode": routing_mode,
        "provider_allowlist": list(dict.fromkeys(providers)),
        "model_allowlist": list(dict.fromkeys(models)),
        "egress": {
            **DEFAULT_POLICY["egress"],
            **{str(key): str(value) for key, value in egress.items()},
        },
        "budgets": {
            **DEFAULT_POLICY["budgets"],
            **{
                str(key): value
                for key, value in budgets.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
        },
        "artifact_retention": str(
            raw.get("artifact_retention") or "durable_private"
        ),
        "workflow_locks": {str(key): str(value) for key, value in workflow_locks.items()},
        "plugin_locks": {str(key): str(value) for key, value in plugin_locks.items()},
        "experimental": normalize_experimental_flags(experimental),
    }
    return normalized


def load_policy(project_root: str) -> PolicySnapshot:
    root = canonical_project_root(project_root)
    path = _policy_path(root)
    content = _safe_existing_file(path)
    if content is None:
        return PolicySnapshot(
            project_root=root,
            path=str(path),
            exists=False,
            file_sha256=None,
            policy=dict(DEFAULT_POLICY),
        )
    try:
        parsed = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HubV2Error(
            "invalid_policy",
            "The project policy is not valid UTF-8 TOML.",
            scope="policy",
        ) from exc
    return PolicySnapshot(
        project_root=root,
        path=str(path),
        exists=True,
        file_sha256=sha256(content).hexdigest(),
        policy=_normalize_policy(parsed),
    )


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def render_policy(policy: Mapping[str, Any]) -> bytes:
    value = _normalize_policy(policy)
    lines = [
        f"schema = {_toml_string(value['schema'])}",
        f"revision = {value['revision']}",
        f"routing_profile = {_toml_string(value['routing_profile'])}",
        f"routing_mode = {_toml_string(value['routing_mode'])}",
        f"provider_allowlist = {_toml_array(value['provider_allowlist'])}",
        f"model_allowlist = {_toml_array(value['model_allowlist'])}",
        f"artifact_retention = {_toml_string(value['artifact_retention'])}",
        "",
        "[egress]",
    ]
    lines.extend(
        f"{key} = {_toml_string(str(item))}"
        for key, item in sorted(value["egress"].items())
    )
    lines.extend(["", "[budgets]"])
    for key, item in sorted(value["budgets"].items()):
        lines.append(f"{key} = {item}")
    lines.extend(["", "[workflow_locks]"])
    lines.extend(
        f"{_toml_string(key)} = {_toml_string(item)}"
        for key, item in sorted(value["workflow_locks"].items())
    )
    lines.extend(["", "[plugin_locks]"])
    lines.extend(
        f"{_toml_string(key)} = {_toml_string(item)}"
        for key, item in sorted(value["plugin_locks"].items())
    )
    lines.extend(["", "[experimental]"])
    lines.extend(
        f"{key} = {'true' if enabled else 'false'}"
        for key, enabled in sorted(value["experimental"].items())
    )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def prepare_policy_update(
    project_root: str,
    *,
    patch: Mapping[str, Any],
    expected_revision: int,
) -> dict[str, Any]:
    snapshot = load_policy(project_root)
    expected = require_non_negative_int(expected_revision, field="expected_revision")
    if snapshot.policy["revision"] != expected:
        raise HubV2Error(
            "policy_revision_conflict",
            "The project policy revision changed.",
            scope="policy",
            retryable=True,
            safe_details={
                "expected": expected,
                "current": snapshot.policy["revision"],
            },
        )
    proposed = {
        **snapshot.policy,
        **dict(patch),
        "revision": expected + 1,
    }
    for section in (
        "egress",
        "budgets",
        "workflow_locks",
        "plugin_locks",
        "experimental",
    ):
        if section in patch:
            proposed[section] = {
                **snapshot.policy[section],
                **dict(patch[section]),
            }
    normalized = _normalize_policy(proposed)
    rendered = render_policy(normalized)
    proposal = {
        "schema": "agent_hub_policy_update_plan_v2",
        "project_root": snapshot.project_root,
        "path": snapshot.path,
        "base_sha256": snapshot.file_sha256,
        "base_revision": expected,
        "proposed_revision": normalized["revision"],
        "rendered_sha256": sha256(rendered).hexdigest(),
        "rendered_text": rendered.decode("utf-8"),
        "policy": normalized,
    }
    proposal["proposal_sha256"] = sha256(
        render_policy(normalized)
        + str(snapshot.file_sha256 or "").encode("ascii")
        + str(expected).encode("ascii")
    ).hexdigest()
    return proposal


def apply_policy_update(
    project_root: str,
    *,
    proposal: Mapping[str, Any],
    proposal_sha256: str,
) -> PolicySnapshot:
    root = canonical_project_root(project_root)
    if proposal.get("project_root") != root:
        raise HubV2Error(
            "project_scope_mismatch",
            "The policy proposal belongs to a different project.",
            scope="policy",
        )
    if proposal.get("proposal_sha256") != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The policy proposal digest does not match.",
            scope="policy",
        )
    current = load_policy(root)
    if current.file_sha256 != proposal.get("base_sha256"):
        raise HubV2Error(
            "policy_file_conflict",
            "The project policy file changed after preparation.",
            scope="policy",
            retryable=True,
        )
    rendered = str(proposal.get("rendered_text") or "").encode("utf-8")
    if sha256(rendered).hexdigest() != proposal.get("rendered_sha256"):
        raise HubV2Error(
            "proposal_digest_conflict",
            "The rendered policy content changed after preparation.",
            scope="policy",
        )
    path = _policy_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise HubV2Error(
            "unsafe_policy_path",
            "The project policy directory must not be a symlink.",
            scope="policy",
        )
    descriptor, temp_name = tempfile.mkstemp(prefix=".project.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return load_policy(root)
