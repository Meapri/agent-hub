---
name: release-copilot
description: "Use when the user asks Codex to prepare a release, PR description, changelog, release notes, tag plan, GitHub release plan, version summary, or release checklist from a local git repository. This integrated version uses google_antigravity_release_snapshot, google_antigravity_release_draft, and optional google_antigravity_write polishing."
---

# Release Copilot

Use this skill when the requested deliverable is a release artifact or release
workflow.

## When To Use

Use Release Copilot for:

- PR descriptions and reviewer notes from local git changes
- release notes and changelog entries
- version and tag planning
- conventional commit classification
- recommended semantic version bumps
- GitHub compare URLs
- release readiness snapshots
- test or validation summaries for release artifacts

Do not use it for general implementation, debugging, refactoring strategy, or
architecture judgment unless the requested output is a release artifact.

## Tool Preference

Use `google_antigravity_release_snapshot` when Codex needs structured local git
release context.

Use `google_antigravity_release_draft` when the user wants a PR body, release
notes, changelog entry, or release summary. Keep `polish` false for deterministic
drafting; set `polish` true only when the user wants public prose polished
through Antigravity.

Use `google_antigravity_route_model` before polished release prose when the user
asks for model choice or when a capacity fallback would be useful.
Use `google_antigravity_list_models` only when the user asks for a specific
polishing model or when the default writing model fails.

Example:

```json
{
  "repo": ".",
  "polish": false
}
```

## Safety

This integrated skill drafts release artifacts only. It does not create tags,
push tags, or publish GitHub releases. Codex may run those commands only after
explicit user approval and after verifying the working tree, tag name, release
notes, and authentication state.

Run verification commands separately. MCP release tools deliberately do not
accept arbitrary `check_commands`.
