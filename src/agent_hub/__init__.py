"""agent-hub — unified multi-model coding hub.

Phase 1 of the Hermes-style unification: `agent_hub.server` is a single MCP
server that fans in every tool from the co-located packages (orchestrate-codex
conductor + claude/grok/antigravity provider leaves) and delegates each call to
the owning package verbatim, so behavior is byte-identical while the surface is
one server. Later phases dedupe shared infra into `agent_hub.core` and turn each
package into a thin `agent_hub.providers` adapter without changing tool names.
"""

__version__ = "1.3.1"
