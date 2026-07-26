# Notice

This archived plugin snapshot provides a direct Google OAuth Code Assist
transport plus optional compatibility with a user-selected Antigravity token
export. All provider calls require explicit user consent.

The compatibility transport can read a user-selected JSON token export using
the schema demonstrated by Antigravity-Proxy. It does not inspect browser state
or macOS Keychain, scrape an official CLI binary, or vendor proxy runtime code.
Token values are never returned through MCP.

Architecture ideas were informed by the MIT-licensed
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and
the upstream `Meapri/google-antigravity-codex` history and the authentication
boundary demonstrated by
[Meapri/Antigravity-Proxy](https://github.com/Meapri/Antigravity-Proxy). No Hermes source tree,
runtime patch, repair hook, service restart logic, or installed-tree drift
checker is vendored in the release bundle.
