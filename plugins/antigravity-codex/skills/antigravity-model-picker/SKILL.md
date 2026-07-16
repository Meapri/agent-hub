---
name: antigravity-model-picker
description: "Help the user list, compare, and save Antigravity models (default and per-task prefs)."
---

# Antigravity Model Picker

## Workflow

1. `google_antigravity_list_models` — live options when authenticated.
2. `google_antigravity_get_model_prefs` — current saved/effective models.
3. If the user wants a recommendation without saving: `google_antigravity_route_model`.
4. To **persist**:
   - default: `google_antigravity_set_model` `{ "model": "flash" }`
   - per task: `{ "model": "opus", "task": "code" }`
5. Optional side-by-side: `google_antigravity_compare_models` with a short prompt
   and 2–3 models (capped for cost).

## Aliases

`flash`, `pro`, `opus`, `sonnet`, `gpt-oss`, `image`, …

## CLI

```bash
python3 scripts/google_antigravity_model.py list
python3 scripts/google_antigravity_model.py set pro
python3 scripts/google_antigravity_model.py set opus --task code
python3 scripts/google_antigravity_model.py get
```

After set, omit `model` on chat/write/search/image so prefs apply.
