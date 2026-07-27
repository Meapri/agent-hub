"""User-maintained routing priors blended beneath observed routing statistics.

Priors live outside ``routing_samples`` on purpose: a prior is an assumption, not
an observation, and mixing the two would let unverified numbers masquerade as
measured quality. The file is user-global because the observed sample space is
already user-global -- neither ``routing_samples`` nor ``routing_context`` carries
a project, so a project-scoped prior would produce different posteriors for the
same observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import stat
import tempfile
import time
import tomllib
from typing import Any, Callable, Mapping

from .contracts import require_non_negative_int
from .errors import HubV2Error
from .provider_manifests import builtin_provider_manifests
from .store import DEFAULT_STATE_DIR

ROUTING_PRIOR_SCHEMA = "agent_hub_routing_prior_v1"
ROUTING_PRIOR_FILE_NAME = "routing_prior.toml"
MAX_ROUTING_PRIOR_BYTES = 64 * 1024
MAX_ROUTING_PRIOR_ENTRIES = 256
# Expressed in the same units as routing sample signal_weight (one runtime
# sample is 3.0), so the default prior is worth about two observations.
PRIOR_DEFAULT_WEIGHT = 6.0
PRIOR_MAX_WEIGHT = 30.0
PRIOR_DEFAULT_STALE_AFTER_DAYS = 90.0
PRIOR_MAX_STALE_AFTER_DAYS = 365.0
PRIOR_STALE_HALF_LIFE_DAYS = 30.0
PRIOR_MIN_EFFECTIVE_WEIGHT = 0.01
PRIOR_SOURCES = frozenset({"unset", "user_estimate", "user_measurement", "vendor_docs"})
_METRIC_FIELDS = ("quality", "reliability", "latency_ms", "total_tokens")
_TOP_LEVEL_FIELDS = frozenset(
    {"schema", "revision", "collected_at", "prior_weight", "stale_after_days", "entries"}
)
_ENTRY_FIELDS = frozenset(
    {
        "capability",
        "provider",
        "model",
        "source",
        "prior_weight",
        "collected_at",
        # Derived, and always recomputed below, but accepted so a normalized
        # snapshot can be fed back through prepare/render without stripping.
        "effective_weight",
        *_METRIC_FIELDS,
    }
)


def default_routing_prior_path() -> Path:
    return DEFAULT_STATE_DIR / ROUTING_PRIOR_FILE_NAME


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], *, field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise HubV2Error(
            "invalid_routing_prior",
            "The routing prior contains unsupported fields.",
            scope="routing",
            safe_details={"field": field, "unknown": ",".join(unknown)[:128]},
        )


def _unit_interval(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HubV2Error(
            "invalid_routing_prior",
            f"{field} must be a number between 0 and 1.",
            scope="routing",
        )
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise HubV2Error(
            "invalid_routing_prior",
            f"{field} must be a number between 0 and 1.",
            scope="routing",
        )
    return number


def _positive_number(value: Any, *, field: str, maximum: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HubV2Error(
            "invalid_routing_prior",
            f"{field} must be a positive number.",
            scope="routing",
        )
    number = float(value)
    if number <= 0.0 or number > maximum:
        raise HubV2Error(
            "invalid_routing_prior",
            f"{field} must be a positive number.",
            scope="routing",
        )
    return number


def _parse_timestamp(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HubV2Error(
            "invalid_routing_prior",
            f"{field} must be an ISO 8601 timestamp.",
            scope="routing",
        ) from exc
    return text


def _epoch(timestamp: str) -> float:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _effective_weight(
    entry: Mapping[str, Any],
    *,
    weight: float,
    collected_at: str | None,
    stale_after_days: float,
    now: float,
) -> float:
    if entry["source"] == "unset":
        return 0.0
    if all(entry.get(field) is None for field in _METRIC_FIELDS):
        return 0.0
    if collected_at is None:
        return weight
    age_days = max(0.0, (now - _epoch(collected_at)) / 86400.0)
    if age_days <= stale_after_days:
        effective = weight
    else:
        # Decay rather than invalidate, matching the observed-sample half-life.
        effective = weight * 0.5 ** ((age_days - stale_after_days) / PRIOR_STALE_HALF_LIFE_DAYS)
    return effective if effective >= PRIOR_MIN_EFFECTIVE_WEIGHT else 0.0


def _normalize_routing_prior(raw: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    _reject_unknown(raw, _TOP_LEVEL_FIELDS, field="routing_prior")
    schema = str(raw.get("schema") or ROUTING_PRIOR_SCHEMA)
    if schema != ROUTING_PRIOR_SCHEMA:
        raise HubV2Error(
            "invalid_routing_prior",
            "The routing prior schema is not supported.",
            scope="routing",
            safe_details={"schema": schema[:64]},
        )
    revision = require_non_negative_int(raw.get("revision", 0), field="revision")
    file_collected_at = _parse_timestamp(raw.get("collected_at"), field="collected_at")
    file_weight = (
        _positive_number(raw.get("prior_weight"), field="prior_weight", maximum=PRIOR_MAX_WEIGHT)
        or PRIOR_DEFAULT_WEIGHT
    )
    stale_after_days = (
        _positive_number(
            raw.get("stale_after_days"),
            field="stale_after_days",
            maximum=PRIOR_MAX_STALE_AFTER_DAYS,
        )
        or PRIOR_DEFAULT_STALE_AFTER_DAYS
    )
    raw_entries = raw.get("entries") or []
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_ROUTING_PRIOR_ENTRIES:
        raise HubV2Error(
            "invalid_routing_prior",
            "The routing prior entries are not supported.",
            scope="routing",
        )
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_entries:
        if not isinstance(item, Mapping):
            raise HubV2Error(
                "invalid_routing_prior",
                "Each routing prior entry must be a table.",
                scope="routing",
            )
        _reject_unknown(item, _ENTRY_FIELDS, field="entries[]")
        capability = str(item.get("capability") or "")
        provider = str(item.get("provider") or "")
        model = str(item.get("model") or "")
        if not capability or not provider:
            raise HubV2Error(
                "invalid_routing_prior",
                "Each routing prior entry needs a capability and a provider.",
                scope="routing",
            )
        key = (capability, provider, model)
        if key in seen:
            raise HubV2Error(
                "invalid_routing_prior",
                "The routing prior contains duplicate entries.",
                scope="routing",
                safe_details={"capability": capability[:64], "provider": provider[:64]},
            )
        seen.add(key)
        source = str(item.get("source") or "unset")
        if source not in PRIOR_SOURCES:
            raise HubV2Error(
                "invalid_routing_prior",
                "The routing prior source is not supported.",
                scope="routing",
                safe_details={"source": source[:64]},
            )
        entry: dict[str, Any] = {
            "capability": capability,
            "provider": provider,
            "model": model,
            "source": source,
            "quality": _unit_interval(item.get("quality"), field="quality"),
            "reliability": _unit_interval(item.get("reliability"), field="reliability"),
            "latency_ms": _positive_number(
                item.get("latency_ms"), field="latency_ms", maximum=3_600_000.0
            ),
            "total_tokens": _positive_number(
                item.get("total_tokens"), field="total_tokens", maximum=10_000_000.0
            ),
        }
        entry_weight = (
            _positive_number(
                item.get("prior_weight"), field="prior_weight", maximum=PRIOR_MAX_WEIGHT
            )
            or file_weight
        )
        entry_collected_at = (
            _parse_timestamp(item.get("collected_at"), field="collected_at") or file_collected_at
        )
        entry["prior_weight"] = entry_weight
        entry["collected_at"] = entry_collected_at
        entry["effective_weight"] = _effective_weight(
            entry,
            weight=entry_weight,
            collected_at=entry_collected_at,
            stale_after_days=stale_after_days,
            now=now,
        )
        entries.append(entry)
    entries.sort(key=lambda item: (item["capability"], item["provider"], item["model"]))
    return {
        "schema": ROUTING_PRIOR_SCHEMA,
        "revision": revision,
        "collected_at": file_collected_at,
        "prior_weight": file_weight,
        "stale_after_days": stale_after_days,
        "entries": entries,
    }


@dataclass(frozen=True)
class RoutingPriorSnapshot:
    path: str
    state: str
    reason_code: str
    file_sha256: str | None
    revision: int
    collected_at: str | None
    age_days: float | None
    stale: bool
    prior_weight: float
    stale_after_days: float
    entries: tuple[dict[str, Any], ...]

    def public(self) -> dict[str, Any]:
        return {
            "schema": "agent_hub_routing_prior_snapshot_v1",
            "path": self.path,
            "state": self.state,
            "reason_code": self.reason_code,
            "file_sha256": self.file_sha256,
            "revision": self.revision,
            "collected_at": self.collected_at,
            "age_days": self.age_days,
            "stale": self.stale,
            "entry_count": len(self.entries),
            "active_entry_count": sum(
                1 for entry in self.entries if entry["effective_weight"] > 0.0
            ),
            "prior": {
                "schema": ROUTING_PRIOR_SCHEMA,
                "revision": self.revision,
                "collected_at": self.collected_at,
                "prior_weight": self.prior_weight,
                "stale_after_days": self.stale_after_days,
                "entries": [dict(entry) for entry in self.entries],
            },
        }

    def lookup(
        self,
        *,
        capability: str,
        provider: str,
        model: str | None,
    ) -> dict[str, Any] | None:
        """Exact (capability, provider, model) first, then the model wildcard.

        The wildcard matters because routing sample keys embed the model, so a
        provider default-model change otherwise resets every accumulated prior.
        """

        wanted = str(model or "")
        exact = None
        wildcard = None
        for entry in self.entries:
            if entry["capability"] != capability or entry["provider"] != provider:
                continue
            if entry["model"] == wanted and wanted:
                exact = entry
            elif entry["model"] == "":
                wildcard = entry
        for candidate in (exact, wildcard):
            if candidate is None:
                continue
            if candidate["effective_weight"] <= PRIOR_MIN_EFFECTIVE_WEIGHT:
                continue
            if all(candidate.get(field) is None for field in _METRIC_FIELDS):
                continue
            return candidate
        return None


def _absent_snapshot(path: Path, *, reason_code: str, state: str) -> RoutingPriorSnapshot:
    return RoutingPriorSnapshot(
        path=str(path),
        state=state,
        reason_code=reason_code,
        file_sha256=None,
        revision=0,
        collected_at=None,
        age_days=None,
        stale=False,
        prior_weight=PRIOR_DEFAULT_WEIGHT,
        stale_after_days=PRIOR_DEFAULT_STALE_AFTER_DAYS,
        entries=(),
    )


def _safe_existing_file(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HubV2Error(
            "unsafe_routing_prior_path",
            "The routing prior target is not a safe regular file.",
            scope="routing",
        )
    if info.st_size > MAX_ROUTING_PRIOR_BYTES:
        raise HubV2Error(
            "routing_prior_too_large",
            "The routing prior exceeds the size limit.",
            scope="routing",
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HubV2Error(
            "routing_prior_read_failed",
            "The routing prior could not be read.",
            scope="routing",
        ) from exc


def load_routing_prior(
    path: str | Path | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> RoutingPriorSnapshot:
    target = Path(path).expanduser() if path is not None else default_routing_prior_path()
    raw_bytes = _safe_existing_file(target)
    if raw_bytes is None:
        return _absent_snapshot(target, reason_code="prior_absent", state="absent")
    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise HubV2Error(
            "invalid_routing_prior",
            "The routing prior is not valid TOML.",
            scope="routing",
        ) from exc
    now = clock()
    normalized = _normalize_routing_prior(parsed, now=now)
    collected_at = normalized["collected_at"]
    age_days = (
        max(0.0, (now - _epoch(collected_at)) / 86400.0) if collected_at is not None else None
    )
    return RoutingPriorSnapshot(
        path=str(target),
        state="loaded",
        reason_code="prior_loaded",
        file_sha256=sha256(raw_bytes).hexdigest(),
        revision=normalized["revision"],
        collected_at=collected_at,
        age_days=age_days,
        stale=bool(age_days is not None and age_days > normalized["stale_after_days"]),
        prior_weight=normalized["prior_weight"],
        stale_after_days=normalized["stale_after_days"],
        entries=tuple(normalized["entries"]),
    )


def safe_load_routing_prior(
    path: str | Path | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> RoutingPriorSnapshot:
    """Never let a malformed prior file break routing for every run."""

    try:
        return load_routing_prior(path, clock=clock)
    except HubV2Error as exc:
        target = Path(path).expanduser() if path is not None else default_routing_prior_path()
        return _absent_snapshot(target, reason_code=exc.code, state="invalid")


def seed_routing_prior(*, collected_at: str) -> dict[str, Any]:
    """Build an inert template.

    The contract of this function is that no provider performance number ever
    ships in this repository: every seeded entry is ``source = "unset"`` with no
    metrics, so it carries zero weight until a human fills it in.
    """

    entries = [
        {
            "capability": capability,
            "provider": manifest["provider_id"],
            "model": "",
            "source": "unset",
        }
        for manifest in builtin_provider_manifests()
        for capability in sorted(manifest["capabilities"])
    ]
    entries.sort(key=lambda item: (item["capability"], item["provider"], item["model"]))
    return {
        "schema": ROUTING_PRIOR_SCHEMA,
        "revision": 0,
        "collected_at": collected_at,
        "prior_weight": PRIOR_DEFAULT_WEIGHT,
        "stale_after_days": PRIOR_DEFAULT_STALE_AFTER_DAYS,
        "entries": entries,
    }


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _toml_number(value: float) -> str:
    return repr(round(float(value), 6))


def render_routing_prior(prior: Mapping[str, Any]) -> bytes:
    value = _normalize_routing_prior(prior, now=0.0)
    lines = [
        "# Agent Hub routing prior.",
        '# An entry stays inactive until its source is changed away from "unset".',
        f"schema = {_toml_string(value['schema'])}",
        f"revision = {int(value['revision'])}",
    ]
    if value["collected_at"] is not None:
        lines.append(f"collected_at = {_toml_string(value['collected_at'])}")
    lines.append(f"prior_weight = {_toml_number(value['prior_weight'])}")
    lines.append(f"stale_after_days = {_toml_number(value['stale_after_days'])}")
    for entry in value["entries"]:
        lines.append("")
        lines.append("[[entries]]")
        lines.append(f"capability = {_toml_string(entry['capability'])}")
        lines.append(f"provider = {_toml_string(entry['provider'])}")
        lines.append(f"model = {_toml_string(entry['model'])}")
        lines.append(f"source = {_toml_string(entry['source'])}")
        for field in _METRIC_FIELDS:
            if entry.get(field) is not None:
                lines.append(f"{field} = {_toml_number(entry[field])}")
        if entry.get("collected_at") is not None and entry["collected_at"] != value["collected_at"]:
            lines.append(f"collected_at = {_toml_string(entry['collected_at'])}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def prepare_routing_prior_update(
    *,
    patch: Mapping[str, Any],
    expected_revision: int,
    path: str | Path | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    expected = require_non_negative_int(expected_revision, field="expected_revision")
    snapshot = load_routing_prior(path, clock=clock)
    if snapshot.revision != expected:
        raise HubV2Error(
            "routing_prior_revision_conflict",
            "The routing prior revision changed.",
            scope="routing",
            retryable=True,
            safe_details={"expected": expected, "current": snapshot.revision},
        )
    base = snapshot.public()["prior"]
    if snapshot.state == "absent" and not patch.get("entries"):
        collected = datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat()
        base = seed_routing_prior(collected_at=collected)
    merged = {key: value for key, value in base.items() if key != "schema"}
    for key, value in patch.items():
        if key in {"schema", "revision"}:
            continue
        # entries replaces the whole list; partial merge would make the digest
        # fence meaningless.
        merged[key] = value
    merged["schema"] = ROUTING_PRIOR_SCHEMA
    merged["revision"] = expected + 1
    normalized = _normalize_routing_prior(merged, now=clock())
    rendered = render_routing_prior(normalized)
    proposal = {
        "schema": "agent_hub_routing_prior_update_plan_v1",
        "path": snapshot.path,
        "base_sha256": snapshot.file_sha256,
        "base_revision": snapshot.revision,
        "proposed_revision": normalized["revision"],
        "rendered_sha256": sha256(rendered).hexdigest(),
        "rendered_text": rendered.decode("utf-8"),
        "prior": normalized,
    }
    proposal["proposal_sha256"] = sha256(
        rendered + str(snapshot.file_sha256 or "").encode("ascii") + str(expected).encode("ascii")
    ).hexdigest()
    return proposal


def apply_routing_prior_update(
    *,
    proposal: Mapping[str, Any],
    proposal_sha256: str,
    path: str | Path | None = None,
    clock: Callable[[], float] = time.time,
) -> RoutingPriorSnapshot:
    if proposal.get("proposal_sha256") != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The routing prior proposal digest does not match.",
            scope="routing",
        )
    target = Path(path).expanduser() if path is not None else default_routing_prior_path()
    current = load_routing_prior(target, clock=clock)
    if current.file_sha256 != proposal.get("base_sha256"):
        raise HubV2Error(
            "routing_prior_file_conflict",
            "The routing prior file changed after preparation.",
            scope="routing",
            retryable=True,
        )
    rendered = str(proposal.get("rendered_text") or "").encode("utf-8")
    if sha256(rendered).hexdigest() != proposal.get("rendered_sha256"):
        raise HubV2Error(
            "proposal_digest_conflict",
            "The routing prior proposal digest does not match.",
            scope="routing",
        )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise HubV2Error(
            "unsafe_routing_prior_path",
            "The routing prior directory must not be a symlink.",
            scope="routing",
        )
    descriptor, temp_name = tempfile.mkstemp(prefix=".routing_prior.", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return load_routing_prior(target, clock=clock)
