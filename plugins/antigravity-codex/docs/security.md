# Security boundaries

## Trust model

Google Antigravity Codex separates three responsibilities:

1. deterministic local MCP and release helpers;
2. native `agy` installation, diagnostics, and an explicitly invoked CLI
   bridge; and
3. model-facing calls through an official `agy` subprocess or an explicitly
   selected, consented `agy` JSON token export.

There is no plugin-owned OAuth authorization-code flow and no plugin credential
store. The `agy-oauth` compatibility provider may invoke the official CLI and
read a user-selected JSON token export using the schema demonstrated by
Antigravity-Proxy. It does not obtain OAuth client credentials or inspect
browser state, macOS Keychain, or the CLI binary.

Consent is recorded locally or supplied through explicit environment flags.
MCP can report consent but cannot modify it.

## agy-owned session providers

`agy-cli` delegates the complete authenticated request to the official CLI.
Plan prompts run in a private disposable directory with a scrubbed child
environment. This is the normal fallback on current macOS installations where
`agy 1.1.2` keeps its session outside the proxy-compatible token path.

`agy-oauth` reads only `GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE`, defaulting to
`~/.gemini/antigravity-cli/antigravity-oauth-token`. The file must be:

- a regular, non-symlink file owned by the current user;
- mode `0600` or stricter on POSIX;
- no larger than 1 MiB; and
- valid JSON containing a non-empty access token.

Credentials remain in memory and are sent only to the fixed
`https://cloudcode-pa.googleapis.com` endpoint. Redirects and ambient proxy
settings are blocked, response sizes are bounded, and upstream error bodies
are omitted. Status returns only presence booleans, expiry state, and project
ID presence.

`agy-cli` is the only automatic provider. `agy-oauth` must be selected
explicitly with `GOOGLE_ANTIGRAVITY_PROVIDER=agy-oauth`; merely creating a
token-export file never changes the active transport. Native grounding and
image generation are rejected on `agy-cli` because its text bridge does not
forward hosted tools or image bytes.

If an official CLI refresh succeeds without creating the token export, the
plugin reports `agy_token_export_unavailable`. It does not fall through to
Keychain extraction. The user can select `agy-cli` instead.

## Local file access

MCP callers are untrusted. File, workspace, project, release, and CLI paths are
resolved before access checks. The server rejects filesystem roots, the whole
user home, path escapes, and known-sensitive paths such as `.env`, `.ssh`,
`.aws`, `.gnupg`, `.kube`, credential files, and private keys.

Source files are bounded. Git context is redacted and truncated. MCP release
schemas do not accept arbitrary commands; the lower-level Python API uses
tokenized arguments without a shell and only after a separate opt-in.

## CLI recursion and mutation

The child process receives `GOOGLE_ANTIGRAVITY_CLI_BRIDGE_DEPTH=1`; recursive
bridge calls are rejected. The native plugin disables CLI bridge, refresh,
chat, grounding, image, and writing tools so `agy -> MCP -> agy` cannot recurse.

Plan mode runs in a disposable directory. Mutating `accept-edits` requires an
explicit workspace and environment opt-in. Unsandboxed plan mode is blocked.
Before invoking `agy`, the bridge removes common secret, credential,
SSH/GPG-agent, `CODEX_`, and `MCP_` environment variables.

Prompt text passed to `agy --print` can be visible to same-user process
inspection. Never submit secrets or raw credential files.

## Image and network handling

Generated inline image data is validated and bounded before writing. Remote
image URLs, when returned, must use HTTPS, resolve to globally routable
addresses, survive redirect revalidation, match an image MIME allowlist, and
fit the configured size limit.

DNS rebinding cannot be completely eliminated with the standard Python URL
stack. Disable remote image URL handling in hostile multi-tenant environments.

## Reporting

Report vulnerabilities privately as described in [SECURITY.md](../SECURITY.md).
Never include tokens, keys, cookies, authorization headers, or raw credentials
in a public issue.
