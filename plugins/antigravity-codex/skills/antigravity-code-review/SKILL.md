---
name: antigravity-code-review
description: "Code review with optional Antigravity: prefer google_antigravity_review_diff on git changes; Codex verifies findings; optional writing polish pass."
---

# Antigravity Code Review

Use this skill for code review with a strict evidence boundary: Codex discovers
and verifies every technical finding; Antigravity may only turn the completed
finding set into clear prose.

## Workflow

1. Inspect `git status`, the complete relevant diff, surrounding code, tests,
   call sites, and configuration locally in Codex.
   Optional assist: `google_antigravity_review_diff` for a first-pass opinion —
   still re-verify every claim in Codex before presenting.
2. Discover bugs, regressions, missing tests, and security risks in Codex. Do
   not treat Antigravity output as authoritative without local verification.
3. Verify each retained finding against the actual file and assign its final
   severity, file, line, evidence, and remediation in Codex.
4. If prose help is useful, send only the verified finding records and residual
   test gaps to `google_antigravity_write` with `task: "technical-doc"`. If the
   configured Google writing provider is unavailable, Codex may use consented
   `google_antigravity_cli_chat` in sandboxed plan mode for prose only.
5. Recheck the returned prose in Codex. Remove invented claims, changed
   severities, unsupported commands, incorrect file/line references, and model
   meta-commentary before presenting it.

## Review Prompt Shape

Give Antigravity an immutable, already verified finding set:

```json
{
  "task": "technical-doc",
  "model": "gemini-3.1-pro-preview",
  "instruction": "Format these verified findings clearly. Preserve every severity, file, line, fact, and test gap exactly. Do not add findings or technical claims.",
  "source_text": "<verified finding records from Codex>",
  "project_context": "off",
  "retry_count": 1,
  "timeout_sec": 180
}
```

## Output Rules

Lead with confirmed findings ordered by Codex-assigned severity. Include only
file/line references verified locally. If no confirmed issues remain, say that
clearly and mention residual test gaps without asking Antigravity to invent a
review narrative.

## Boundaries

- Do not send secrets, raw credential material, or unrelated private code.
- Do not send raw repository content when verified finding records are enough.
- Antigravity must not originate findings, change severity, or decide whether
  a claim is technically correct.
- Do not include speculative or model-originated findings that Codex did not
  verify locally.
