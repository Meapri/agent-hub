# Security boundaries

## Trust model

Google Antigravity Codex separates three responsibilities:

1. deterministic local MCP and release helpers;
2. a visible, consented Google OAuth PKCE login owned by the plugin; and
3. model-facing Code Assist calls through the `agy-oauth` provider.

The login flow stores its token file under the plugin config directory. It may
also read a user-selected JSON token export using the schema demonstrated by
Antigravity-Proxy. It does not inspect browser state, macOS Keychain, or an
official CLI binary.

Consent is recorded locally or supplied through explicit environment flags.
MCP can report consent but cannot modify it.

## Code Assist session provider

`agy-oauth` reads only `GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE`, defaulting to
the plugin OAuth token file. A configured compatibility export must be:

- a regular, non-symlink file owned by the current user;
- mode `0600` or stricter on POSIX;
- no larger than 1 MiB; and
- valid JSON containing a non-empty access token.

Credentials remain in memory and are sent only to the fixed
`https://cloudcode-pa.googleapis.com` endpoint. Redirects and ambient proxy
settings are blocked, response sizes are bounded, and upstream error bodies
are omitted. Status returns only presence booleans, expiry state, and project
ID presence.

`agy-oauth` is the only supported model transport. Legacy `agy-cli` provider
preferences are rejected with a migration error rather than silently changing
the authentication boundary.

## Local file access

MCP callers are untrusted. File, workspace, project, release, and CLI paths are
resolved before access checks. The server rejects filesystem roots, the whole
user home, path escapes, and known-sensitive paths such as `.env`, `.ssh`,
`.aws`, `.gnupg`, `.kube`, credential files, and private keys.

Source files are bounded. Git context is redacted and truncated. MCP release
schemas do not accept arbitrary commands; the lower-level Python API uses
tokenized arguments without a shell and only after a separate opt-in.

## OAuth mutation boundary

Status and model operations are read-only. Login, refresh, logout, and consent
changes require an explicit local GUI or command initiated by the user. MCP
status responses never contain access tokens, refresh tokens, authorization
codes, client secrets, or raw upstream error bodies.

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
