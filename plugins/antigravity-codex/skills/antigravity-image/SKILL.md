---
name: antigravity-image
description: "Generate images through the consent-gated Google Antigravity image tools and inspect image-model availability."
---

# Antigravity Image

After confirming explicit consent, use `google_antigravity_route_model` for
model choice and `google_antigravity_generate_image` for generation. Use
provider and model status when availability matters. Quota status reports that
unified provider buckets are unavailable; it does not invent capacity.

Image generation requires **`agy-oauth`**. If login is missing, run the direct
OAuth flow (`login_start` → browser → `login_complete` with probe) first.
Do not call the image tool through `plugin-OAuth` or present static model names as
live availability.

Return the absolute generated path, MIME type, byte size, model, and warnings.
Do not promise reliable typography, logo accuracy, or legal clearance.
