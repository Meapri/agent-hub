"""Compatibility shim — implementation in agent_hub.core.consent_cli."""

from agent_hub.core import consent_cli as _core
from . import security


def main(argv=None) -> int:
    return _core.run(security, "grok_codex_consent.py", argv)


if __name__ == "__main__":
    raise SystemExit(main())
