"""Agent Hub's canonical public MCP and multi-provider orchestration package.

Claude, Grok, Gemini, and GPT are registered behind the same ``agent_hub_*``
contract. Provider-specific leaf tool names remain private implementation
details, while run lifecycle, handoff, policy, and result normalization stay
provider-neutral.
"""

__version__ = "2.2.0"
