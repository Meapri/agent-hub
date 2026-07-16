---
name: antigravity-model-router
description: "Use when choosing, saving, or routing Antigravity models (default/per-task prefs, list, fallbacks) for chat, code, writing, search, release, image, or fast workflows."
---

# Antigravity Model Router + Selection

## Prefer saved models

1. `google_antigravity_get_model_prefs` — see default + per-task effective models.
2. If the user asks to **use / switch / set** a model:
   - Optionally `google_antigravity_list_models` for live options.
   - `google_antigravity_set_model` with `{ "model": "…", "task": "code" }`  
     or omit `task` for the **global default**.
3. Clear with `google_antigravity_clear_model_prefs` (`task` or `all: true`).

Aliases accepted: `flash`, `pro`, `opus`, `sonnet`, `gpt-oss`, `image`, …

CLI:

```bash
python3 scripts/google_antigravity_model.py list
python3 scripts/google_antigravity_model.py set flash
python3 scripts/google_antigravity_model.py set opus --task code
python3 scripts/google_antigravity_model.py get
```

## Routing for a task

- `google_antigravity_route_model` for task routing across `chat`, `code`,
  `fast`, `grounded-search`, `writing`, `release`, and `image`.
- Honors **saved prefs** then static candidates; `preferred_model` on the call wins.
- Treat `required_provider: agy-oauth` as hard for grounded search and image.
- On capacity/rate-limit failures, retry only listed fallback candidates.

## Response handling

Use `recommended_model`, `selection_source` (`user-pref` / `call-preferred` /
`route-default`), `candidates`, `tool`, and `arguments_template` for the next
MCP call. Chat/write/search/image pick the saved model when `model` is omitted.

## Boundaries

- Do not invent model ids; use list/route/set output.
- Do not turn routing into a long debate unless the user wants a comparison.
