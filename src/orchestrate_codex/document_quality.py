"""Deterministic checks for durable Korean documentation.

These checks catch recurring copy failures. They do not attempt to score writing
quality or replace a human review.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Iterable, Sequence


TRANSLATION_LIKE_PHRASES = (
    "이전 이름은 지원하지 않습니다",
    "끝난 것입니다",
    "별개로 보면 됩니다",
    "실패하는 것이 정상입니다",
    "호출 예산",
    "이를 통해",
    "활용할 수 있습니다",
    "dependency frontier",
)

PROCESS_NARRATION_PATTERNS = (
    re.compile(
        r"(?:먼저|이제|다음으로|마지막으로).{0,40}"
        r"(?:하겠습니다|살펴보겠습니다|알아보겠습니다|정리해보겠습니다)"
    ),
    re.compile(r"(?:이 문서|이 글)에서는.{0,40}(?:살펴봅니다|알아봅니다)"),
)


def review_natural_korean(text: str) -> list[str]:
    """Return stable warning codes for known translationese and process narration."""

    warnings: list[str] = []
    for phrase in TRANSLATION_LIKE_PHRASES:
        if phrase in text:
            warnings.append(f"korean_style:translation_like:{phrase}")
    for index, pattern in enumerate(PROCESS_NARRATION_PATTERNS, start=1):
        match = pattern.search(text)
        if match:
            warnings.append(f"korean_style:process_narration:{index}:{match.group(0)}")
    return warnings


def review_paths(paths: Iterable[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            findings.append(f"{path}:read_error:{exc}")
            continue
        findings.extend(f"{path}:{warning}" for warning in review_natural_korean(text))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check durable Korean documents for known translationese and process narration."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    findings = review_paths(args.paths)
    if findings:
        print("Document quality check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"Document quality check passed ({len(args.paths)} file(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
