"""Shared policy provenance and fail-closed multi-provider decision gates."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence


DEFAULT_MAX_POLICY_CHARS = 100_000
DEFAULT_DECISION_THRESHOLD = 1.0
DEFAULT_MIN_RESPONSES = 2
POLICY_CANDIDATES = ("AGENTS.md", "CLAUDE.md")
_DECISION_KEYS = {"schema", "label", "confidence", "rationale", "evidence", "uncertainties"}
_FENCED_JSON_RE = re.compile(r"\A```(?:json)?\s*\n?(.*?)\n?```\s*\Z", re.I | re.S)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def load_policy(
    *,
    project_root: str,
    policy_file: str = "",
    required: bool = False,
    max_chars: int = DEFAULT_MAX_POLICY_CHARS,
) -> Dict[str, Any]:
    """Load one canonical project policy without allowing paths outside the project root."""

    root = Path(project_root or ".").expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    limit = int(max_chars)
    if limit < 1 or limit > 1_000_000:
        raise ValueError("max_policy_chars must be between 1 and 1000000")

    if policy_file:
        requested = Path(policy_file).expanduser()
        candidate = (requested if requested.is_absolute() else root / requested).resolve()
        if not _inside(root, candidate):
            raise ValueError("policy_file must stay inside project_root")
        candidates = [candidate]
    else:
        candidates = [(root / name).resolve() for name in POLICY_CANDIDATES]

    selected = next((path for path in candidates if path.is_file()), None)
    if selected is None:
        if required:
            names = policy_file or " or ".join(POLICY_CANDIDATES)
            raise ValueError(
                f"canonical policy is required but missing under project_root: {names}"
            )
        return {
            "loaded": False,
            "source": None,
            "sha256": None,
            "chars": 0,
            "text": "",
        }

    raw = selected.read_text(encoding="utf-8")
    text = _normalize_text(raw)
    if len(text) > limit:
        raise ValueError(f"canonical policy exceeds max_policy_chars: {len(text)} > {limit}")
    return {
        "loaded": True,
        "source": str(selected),
        "sha256": _digest(text),
        "chars": len(text),
        "text": text,
    }


def prepare_provider_call(arguments: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Inject the same canonical policy and attach reproducible request provenance."""

    call_args = dict(arguments)
    mode = str(call_args.pop("policy_mode", "auto") or "auto").strip().lower()
    if mode not in {"off", "auto", "required"}:
        raise ValueError("policy_mode must be off, auto, or required")
    project_root_value = call_args.pop("project_root", None)
    policy_file = str(call_args.pop("policy_file", "") or "").strip()
    max_chars = int(call_args.pop("max_policy_chars", DEFAULT_MAX_POLICY_CHARS))

    policy: Dict[str, Any] = {
        "loaded": False,
        "source": None,
        "sha256": None,
        "chars": 0,
        "text": "",
    }
    should_load = mode == "required" or bool(project_root_value or policy_file)
    if mode != "off" and should_load:
        policy = load_policy(
            project_root=str(project_root_value or "."),
            policy_file=policy_file,
            required=mode == "required",
            max_chars=max_chars,
        )
    if policy["loaded"]:
        existing = str(call_args.get("system") or "").strip()
        policy_block = (
            "The following canonical policy governs behavior and process. It is not repository "
            "evidence. Product facts inside it must still be verified against supplied source or "
            "a deterministic fact pack; when they conflict, current repository evidence wins.\n"
            f'<agent-hub-canonical-policy sha256="{policy["sha256"]}">\n'
            f"{policy['text']}</agent-hub-canonical-policy>"
        )
        call_args["system"] = "\n\n".join(part for part in (existing, policy_block) if part)

    request_material = json.dumps(
        {
            "prompt": call_args.get("prompt"),
            "messages": call_args.get("messages"),
            "system": call_args.get("system"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    provenance = {
        "policy_mode": mode,
        "policy_loaded": bool(policy["loaded"]),
        "policy_source": policy["source"],
        "policy_sha256": policy["sha256"],
        "policy_chars": policy["chars"],
        "request_sha256": _digest(request_material),
    }
    return call_args, provenance


def validate_labels(values: Iterable[Any]) -> List[str]:
    labels = [str(item).strip() for item in values if str(item).strip()]
    if not 2 <= len(labels) <= 20:
        raise ValueError("decision_labels must contain between 2 and 20 labels")
    if len(set(labels)) != len(labels):
        raise ValueError("decision_labels must be unique")
    if any(len(label) > 64 for label in labels):
        raise ValueError("decision labels must not exceed 64 characters")
    return labels


def decision_prompt(prompt: str, labels: Sequence[str]) -> str:
    allowed = json.dumps(list(labels), ensure_ascii=False)
    return (
        f"{prompt.strip()}\n\n"
        "Return exactly one JSON object and no markdown or trailing prose. "
        "Prefer the minimal contract so the response cannot be cut off: "
        '{"schema":"decision_v1","label":<one allowed label>,'
        '"confidence":<number 0..1>}. '
        "Optional allowed fields are rationale (short string), evidence (string array), and "
        "uncertainties (string array); omit them unless they are necessary. "
        f"Allowed labels (case-sensitive): {allowed}. "
        "Do not create a new label. Keep rationale under 500 characters and each list at 8 items or fewer."
    )


def parse_decision(text: str, labels: Sequence[str]) -> Dict[str, Any]:
    body = str(text or "").strip()
    fenced = _FENCED_JSON_RE.fullmatch(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid decision_v1 JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("decision_v1 response must be one JSON object")
    keys = set(value)
    if not {"schema", "label", "confidence"}.issubset(keys):
        raise ValueError("decision_v1 requires schema, label, and confidence")
    if keys - _DECISION_KEYS:
        raise ValueError(
            f"decision_v1 contains unsupported fields: {sorted(keys - _DECISION_KEYS)}"
        )
    if value.get("schema") != "decision_v1":
        raise ValueError("schema must equal decision_v1")
    if value.get("label") not in labels:
        raise ValueError("label is not in decision_labels")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    for key in ("rationale",):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"{key} must be a string")
    for key in ("evidence", "uncertainties"):
        if key in value and (
            not isinstance(value[key], list)
            or len(value[key]) > 8
            or any(not isinstance(item, str) for item in value[key])
        ):
            raise ValueError(f"{key} must be an array of at most 8 strings")
    return value


def evaluate_decisions(
    results: Sequence[Mapping[str, Any]],
    *,
    threshold: float = DEFAULT_DECISION_THRESHOLD,
    require_all: bool = True,
    min_responses: int = DEFAULT_MIN_RESPONSES,
) -> Dict[str, Any]:
    """Calculate a deterministic label vote without pretending to score open text."""

    threshold_value = float(threshold)
    if not 0.5 <= threshold_value <= 1.0:
        raise ValueError("threshold must be between 0.5 and 1.0")
    minimum = int(min_responses)
    if minimum < 2:
        raise ValueError("min_responses must be at least 2")

    total = len(results)
    valid = [item for item in results if isinstance(item.get("decision"), dict)]
    provider_successes = sum(bool(item.get("success")) for item in results)
    votes = Counter(str(item["decision"]["label"]) for item in valid)
    ranked = votes.most_common()
    top_count = ranked[0][1] if ranked else 0
    tied = bool(len(ranked) > 1 and ranked[1][1] == top_count)
    winner = ranked[0][0] if ranked and not tied else None
    agreement = top_count / len(valid) if valid else 0.0
    coverage = len(valid) / total if total else 0.0

    reasons: List[str] = []
    if provider_successes < total:
        reasons.append("provider_failure")
    if len(valid) < provider_successes:
        reasons.append("invalid_contract")
    if len(valid) < minimum:
        reasons.append("insufficient_valid_responses")
    if require_all and len(valid) < total:
        reasons.append("require_all_not_met")
    if tied or (valid and agreement < threshold_value):
        reasons.append("decision_disagreement")

    passed = bool(
        winner
        and len(valid) >= minimum
        and agreement >= threshold_value
        and (not require_all or len(valid) == total)
    )
    if not passed and not reasons:
        reasons.append("no_consensus")
    return {
        "enabled": True,
        "contract": "decision_v1",
        "passed": passed,
        "human_review": not passed,
        "decision": winner if passed else None,
        "candidate_decision": winner,
        "agreement_score": round(agreement, 6),
        "coverage": round(coverage, 6),
        "threshold": threshold_value,
        "require_all": bool(require_all),
        "min_responses": minimum,
        "requested_responses": total,
        "provider_successes": provider_successes,
        "valid_responses": len(valid),
        "votes": dict(votes),
        "review_reasons": reasons,
    }
