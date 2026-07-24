"""CLI entry point for GPT provider consent."""

from agent_hub.core import consent_cli as _core

from . import security


def main(argv=None) -> int:
    return _core.run(security, "openai_codex_consent.py", argv)


if __name__ == "__main__":
    raise SystemExit(main())
