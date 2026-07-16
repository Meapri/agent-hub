---
name: antigravity-profiles
description: "Switch Antigravity session profiles (coding, writing, research, fast, pair, balanced) bundling model, grounding, and thinking."
---

# Session Profiles

## Built-in

| Profile | Intent |
|---------|--------|
| `balanced` | Default chat |
| `coding` | Stronger code/architecture |
| `writing` | Prose / docs |
| `research` | Grounded search (needs agy-oauth) |
| `fast` | Low latency |
| `pair` | Second-opinion strong model |

## Tools

- `google_antigravity_list_profiles`
- `google_antigravity_use_profile` `{ "name": "coding" }`
- `google_antigravity_save_profile` — custom profiles
- Clear active: `use_profile` with empty name

Activating a profile may also set model prefs and preferred provider.
Chat then inherits model/grounding/thinking when those fields are omitted.
