# Notice

This plugin provides an official `agy` CLI transport plus an optional
compatibility transport for an `agy`-owned Antigravity token export. All
provider calls require explicit user consent.

The compatibility transport can read a user-selected JSON token export using
the schema and default path used by Antigravity-Proxy. It does not include or
derive an OAuth client, inspect browser state or macOS Keychain, scrape the
official CLI binary, or vendor proxy runtime code. Token values are never
returned through MCP.

Architecture ideas were informed by the MIT-licensed
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and
the upstream `Meapri/google-antigravity-codex` history and the authentication
boundary demonstrated by
[Meapri/Antigravity-Proxy](https://github.com/Meapri/Antigravity-Proxy). No Hermes source tree,
runtime patch, repair hook, service restart logic, or installed-tree drift
checker is vendored in the release bundle.
