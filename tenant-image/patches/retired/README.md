# Retired from the v2026.9.24 pilot image

These historical patches are **not applied**. The old v2026.8.3 image remains the
rollback artifact; never apply this directory as an extra overlay.

- **001:** v2026.9.24 owns collision-safe `_oauth_wire_namer`, `_normalize_to_mcp_wire`,
  explicit `session_search`/`memory` aliases, prose-safe substitution and response
  reversal. The old deep scrub changes actual paths and arbitrary replayed
  user/tool payloads, conflicting with upstream's path-preservation fix. The
  compatibility gate tests actual pinned source. Live OAuth billing still needs
  the founder credential acceptance check; local tests cannot prove billing.
- **002/003:** arbitrary session-key approval selection is replaced by the
  pilot's separate-runtime owner approval and explicit result-release flow.
  Group agents receive no access to private sessions or credentials.
- **004:** the pilot never starts the legacy Baileys WhatsApp adapter. wacli is
  the sole WhatsApp socket owner and receipt authority. Patching the inactive
  Baileys bridge would not verify or fix pilot receipts.

005 retains product notices/onboarding behavior at the new gateway module
locations. 006 retains optional Telegram sleep settings while preserving the
upstream default when the Squire variable is absent.
