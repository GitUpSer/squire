# Founder offline transformation gates

`infra/pilot_founder_transform.py` operates on decrypted **offline copies** of the verified encrypted archives. It never changes the source agent, starts a target runtime, authorizes a group, or resumes a scheduled job. Run with the pinned Hermes 9.24 source and runtime Python (SQLite at least the source's 3.50.4); group import checks the exact Hermes distribution version 0.21.5.

The target layout is `HOME=HERMES_HOME=/opt/data`. Map source `.hermes/` contents directly into `/opt/data`, with gateway routing scope `/opt/data/sessions`. Do not introduce `/opt/data/.hermes`. External owner integration paths must be reviewed and translated individually.

## Implemented transformations

- `cron-inventory SOURCE OUTPUT` records source file/schema hashes and job fingerprints without printing prompts, script contents or credentials.
- `cron-transform SOURCE OUTPUT PLAN` requires those exact hashes and an explicit assignment of each selected job to a private runtime or an approved group's runtime. DM jobs preserve reviewed private context; scripts/workdirs require explicit absolute target paths. Group jobs require a new sanitized prompt/name and receive only `memory`, the registered `squire-group` approval toolset, and the real Hermes `no_mcp` scheduler sentinel. Group output never contains source private prompts, script bodies, delivery content, incidents or notepad state.
- Cron execution IDs and scheduled instants remain stable. Claimed/running executions and pending/uncertain deliveries become `unknown`; historical receipts remain. Every transformed job is paused. Ordinary upstream `resume_job` can retain an overdue occurrence, so **do not bulk-resume**: reconcile unknown effects/deliveries and explicitly review the next future occurrence first. Unassigned jobs remain in the source archive.
- Existing `infra/migrate_personal_agent.py remap-private-state` accepts multiple reviewed historical DM sessions for one conversation while requiring exactly one active routing session. It preserves selected parent links. The snapshot contains older untyped WhatsApp sessions without chat identity: these remain archived until identity evidence is available; guessing an owner/group mapping is prohibited.
- `group-history` creates a fresh Hermes database from the exporter’s checksummed visible user/assistant messages only, with an explicit conversation and approved-claim reference. It imports no private memory, credentials, tool traces or system prompts. The operator must verify that the claim remains active in the broker; a local claim string is not authorization.
- `memory-config SOURCE OUTPUT --expected-sha256 HASH` preserves the existing Hindsight bank ID and all settings except the API URL, which becomes loopback port 9177. Reports contain a bank identity hash rather than the bank name. Founder memory must use the verified 0.5.2 server/client 0.10.3 compatibility image and local BGE-small 384 embeddings; the attempted 0.9 migration failed content parity and must not be used for founder memory.
- `private-environment` removes legacy transport and control capabilities from a checksummed `.env` copy, retaining reviewed owner model/tool credentials. Multiline/unrecognized assignments require manual preparation. `runtime-config` keeps owner identity/model/memory but enables only the Squire platform and overlay, removes legacy hooks/transports, and preserves only explicitly named MCP servers and target skill paths. Direct-send MCP definitions are rejected.
- `promote-private` copies a positive per-file list into a new offline home. Every entry needs a category, reviewed checksum and `reviewed_no_direct_transport: true`. Prepared config/env/memory-config replace old transport settings. Legacy WhatsApp/Telegram/Baileys state and unreviewed plugins are rejected. Unknown scripts are never automatically copied or enabled. The final write is the checked restored-home manifest consumed by the actual entrypoint adoption validator.

## Promotion plan contract

The plan has `kind: dm`, `runtime_id`, `migration_id`, an immutable `image_ref` ending in `@sha256:<64 hex>`, and `files` entries:

```json
{
  "root": "snapshot",
  "source": ".hermes/scripts/report.py",
  "destination": "scripts/report.py",
  "sha256": "<reviewed-content-hash>",
  "category": "workspace",
  "reviewed_no_direct_transport": true,
  "source_mode": 493
}
```

Mode 493 is octal 0755. Executability is restored only when the plan's `source_modes` descriptor (`root`, `path`, `sha256`) identifies the verified source-mode JSON and its matching path record agrees on content hash, size and mode. A restored file's local mode 600 does not prove that it was originally non-executable. Only reviewed workspace/skill files may become owner-executable; group/other permissions are removed. Omit `source_mode` to retain 0600. An optional `source_mode_path` selects the exact original metadata key after a reviewed source-layout translation.

Required promoted files are `config.yaml`, `SOUL.md`, `hindsight/config.json`, and `plugins/hindsight/__init__.py`. Additional histories, cron output, credentials and integration code require explicit entries. Credential files remain private-owner-only and mode 0600. The report distinguishes `.env`/`auth.json` covered by the existing application envelope from other owner-only credential files; it does not claim universal application-level encryption. Transport archives are encrypted in transfer and private integration credentials must never be promoted into groups.

Raw `.camofox` files are excluded from generic promotion. Import the reviewed `profiles`, `cookies`, `checkpoints` and `private` directories through `BrowserState.import_from()` with the target runtime DEK, producing encrypted browser state while live profiles use tmpfs. Preserve the source archive until parity is verified.

First adoption requires `SQUIRE_RESTORED_HOME=true`, `SQUIRE_HINDSIGHT_VARIANT=founder`, `SQUIRE_RUNTIME_KIND=dm`, `HERMES_SKIP_CONFIG_MIGRATION=1`, matching `TENANT_ID` and `SQUIRE_IMAGE_REF`. Provisioning sets the founder-only flags; the manifest validator rejects mismatched or modified critical files before normal initialization. No manual `.squire/initialized` shortcut is needed.

## Remaining live gates

These transformations are implementation and offline-test results, not a completed cutover. The operator still needs to review the actual file/job/session mapping, produce the final source-fenced snapshot, verify checksums and terminal/uncertain work, promote only the reviewed output into the founder volume, and start a restored-home canary with Hindsight background workers paused. Verify original bank identity, content counts/hashes, chat continuity, provider access, browser login, an explicitly approved private action, scoped scheduled delivery and group isolation. Restore/rollback rehearsal and the planned soak gate must pass before retiring the old account socket. Any ambiguous old session or direct-send script remains quarantined until it has a safe reviewed mapping.

Validation command:

```sh
HERMES_SOURCE=/path/to/pinned/hermes PYTHONPATH=infra:/path/to/pinned/hermes /path/to/runtime-venv/bin/python -m pytest infra/test_pilot_founder_transform.py tenant-image/overlay/tests/test_onboarding.py -q
```
