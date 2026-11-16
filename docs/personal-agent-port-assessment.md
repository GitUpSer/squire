# Porting the personal agent's overlay changes to Squire — assessment (2026-10-18)

Written after the 2026-10-18 session on the founder's personal agent (`hermes-agent-bootstrap`,
releases `502c3b9d0` → `be3f3d797`, plugin 0.4.1 → 0.5.1). The personal agent is the blueprint; the
goal is that every good change to it lives in the Squire layer above the NousResearch harness
(`tenant-image/overlay/`, Phase ③), so that the personal agent can later become the first Squire-spawned
tenant. **This is an assessment only. Nothing here has been implemented in Squire.**

Companion documents: `overlay-phase3-plan.md` (the layer this targets), `overlay-migration-handoff.md`
(the model), and in the bootstrap repo `docs/update-log.md` (entries dated 2026-10-18) and
`deploy/resilience/README.md` (isolation design, coverage table, gaps).

## 1. What changed on the personal agent (the candidates)

| # | Change (bootstrap repo) | Where it lives there | One line |
|---|---|---|---|
| C1 | Progress-notice scope: owner DM only, DM-only by construction | `overlay/plugin/hermes_personal/guardrails/progress.py`, managed `guardrails.notify_chats` | The guardrails "⏳ Still working" heartbeat goes only to a DM (upstream `:dm:` session-key slot, not a WhatsApp group/broadcast JID), optionally only to an allow-list; LID/phone twins resolved. |
| C2 | Progress notices scoped to the current task | `guardrails/ledger.py` (`task` block), `progress.py` | Elapsed/children/errors from the current task, not the chat ledger's lifetime; a task retired for age stays retired. |
| C3 | Latency guardrails (merged 2026-10-09, live since) | `overlay/plugin/hermes_personal/guardrails/` | Host-wide browser lease, breaker, deadlines, preflight admission, shared findings, `/guardrails`, the notifier above. |
| C4 | Workload isolation (enabled 2026-10-18) | `overlay/plugin/hermes_personal/isolation/`, `overlay/ops/units/*.slice` | Every gateway-spawned terminal / background / execute_code / LSP process in its own `systemd-run --user --scope` under `hermes-workload.slice` with a hard `MemoryMax`; WhatsApp bridge in `hermes-transport.slice`; fail-closed; gateway-only gate; probe off the event loop. |
| C5 | Stall watchdog + loopback recovery API (enabled 2026-10-18) | `overlay/ops/stall_recovery.py`, `overlay/ops/units/hermes-stall-recovery.service`, `overlay/ops/_watchdog_common.py` | Own user service; "stalled" = loop-tick socket unanswered AND heartbeat file stale, ≥3 strikes; `systemctl --user restart` only, bounded; loopback HTTP page (bearer + nonce) for restart/pause. |
| C6 | Transport "external" mode (code present, NOT enabled) | `isolation/transport.py`, `deploy/resilience/hermes-whatsapp-bridge.service` | Bridge as its own unit; gateway adopts over HTTP, never spawns/kills; identity + parity checks. |
| C7 | Approvals group→DM mirror, ignored chats, stale-tool nudge | `approvals.py`, `ignored_chats.py`, `stale_tool_nudge.py` | Older overlay features, all live. |
| C8 | Patch 004 per-chat tool progress; patches 001–003 | `overlay/patches/` | WhatsApp-specific (mark-online, GAS read-only scope, DM read receipts) + per-chat `tool_progress_chats`. |
| C9 | Process: review/merge workflow, agent-commit guard, hermes-update skill + gotchas, verify checks | `deploy/*.sh`, `.claude/skills/hermes-update/` | How changes get reviewed, gated and proven live. |

## 2. Facts about Squire that decide the verdicts

Verified against the Squire repo and the Hermes v2026.8.3 tree on 2026-10-18.

1. **The overlay layer does not exist yet.** Squire is at Phase ③ step 0a (`infra/fleet_audit*.py`,
   commit 4733ff7). `tenant-image/overlay/` arrives over v0.2.13 → v0.3.0 → v0.3.1, by design
   behaviour-preserving (plan: "Behaviour must match v0.2.12 except for the declared deltas").
   **Nothing below should jump that queue.** Phase ④ (base bump 8.3 → 9.24 via the hermes-update skill)
   is the natural landing zone for plugin features: it is the API the personal overlay was written against.
2. **A tenant is one Railway container**: s6 (PID 1) → `bin/squire-entrypoint.sh` → supervisord → eight
   programs (`tenant-image/supervisord.conf`). There is **no systemd user manager and no cgroup
   delegation**. `systemd-run --scope`, `.slice` files and user `.service` units cannot exist. The
   in-container restart primitive is `supervisorctl restart <program>` (already used by
   `bin/squire-hindsight-healthcheck.py`). A runaway child does not throttle a sub-cgroup; it pushes the
   whole container to its limit and Railway restarts the whole tenant.
3. **The managed layer is fleet-wide.** `/etc/hermes/config.yaml` is baked into the image and identical
   for every tenant. The personal agent hardcodes the owner's chat id in managed config
   (`guardrails.notify_chats`, `display.platforms.whatsapp.tool_progress_chats`, `ignored_chats`). In
   Squire **nothing tenant-specific may live there**: every "owner" concept must be derived at runtime
   from the adopted home channel (`home_adoption.py` in the v0.3.0 plan knows it).
4. **Base is Hermes v2026.8.3 until Phase ④.** Present at 8.3 (`hermes_cli/plugins.py`):
   `register_middleware` (tool middleware via `hermes_cli.middleware.VALID_MIDDLEWARE`),
   `register_command`, `register_hook`, `register_platform`; hooks `pre_tool_call`, `post_tool_call`,
   `pre_llm_call`, `subagent_start`/`subagent_stop`, `pre_gateway_dispatch`, `pre_approval_request`.
   Also present: `hermes_cli/managed_scope.py`, `tools/process_registry.py`, the loop-tick socket and
   `state/gateway.heartbeat` (`gateway/shutdown_watchdog.py`, `gateway/status.py`), the
   `build_session_key` chat_type layout. **Absent at 8.3:** `ctx.get_config` (the ③ plan shims it in
   `_config.py`), `on_unload`, `agent_loop_stopped`, `plugin_compat`.
5. **Telegram-first through the webhook shim**; WhatsApp is now a launch platform (`apps/web` commits
   326936a, 1ada0a7), so the Baileys bridge will eventually run inside the tenant container as a gateway
   child — the same shape the personal agent had before C4/C6.
6. **No per-tenant resource limits are set today** (`control_api/clients/railway.py` sets none); the PRD
   (§5.4) names CPU weight / cron ceilings / storage quota as load-bearing fair-use caps still to come.

## 3. Verdicts

### Port, through the `squire-overlay` plugin, in Phase ④

**C3 + C1 + C2 — guardrails as a whole.** The most valuable thing the personal agent has that tenants
lack. Every mechanism it needs exists at 8.3 except `on_unload` (tolerate: the notifier thread is a
daemon) and `ctx.get_config` (the ③ shim). Changes for Squire:

- `notify_chats` must default to **the tenant's adopted home channel**, read at decision time, never a
  managed literal. The `:dm:` session-key detection (C1) already works for Telegram keys
  (`agent:main:telegram:dm:<id>`); keep it.
- `resource_scope: false` in the managed file (no systemd). Preflight's Chromium count / load / swap
  checks read the container's view of the host; re-baseline the thresholds for a tenant container
  (they were tuned for a 4 GB VPS).
- One container = one tenant, so "owner DM only" and "host-wide browser lease" map cleanly.
- Keep the two lessons from 2026-10-18: a `stale` retirement keeps the task scope; DM detection is
  keyed on upstream's chat_type slot, never on a list of group markers.

**C4 — isolation: port the seams and the policy, replace the backend.** The problem is real for tenants
and arguably worse (fact 2). The four spawn seams (`seams.py`: terminal, execute_code, LSP, background)
and `policy.decide()` are backend-agnostic; `scope.py` is not. For Squire:

- Backend: per-child **rlimits via `preexec_fn`** (`RLIMIT_AS`/`RLIMIT_DATA`, optionally `RLIMIT_CPU`)
  with the same per-class table; or a tiny supervisord "workload reaper" that kills the largest
  non-gateway child when container RSS approaches the limit. Both are weaker than a cgroup kill but
  strictly better than nothing; neither needs privileges.
- `enforce: false` for tenants (degrade, never refuse): a refused terminal command is a product
  failure; on the VPS it is an operator signal.
- **The gateway-only gate must change.** It keys on upstream's `_is_supervised_gateway_process()`,
  which needs the `HERMES_SUPERVISED_CHILD`/supervisor markers a generated launcher exports. Under
  supervisord the gateway is started by `/opt/hermes/.venv/bin/hermes gateway run --no-supervise`
  with no such marker, so the gate would read "not the gateway" forever and the policy would be
  inert. Gate on supervisord's own environment (`SUPERVISOR_PROCESS_NAME=gateway`) instead.
- **Cheapest first step, available in ③'s managed file:** disable LSP auto-spawn for tenants
  (the lint hook's TypeScript trees were the 10-16 trigger). That removes the incident's cause
  before any seam ships.

### Port in spirit, as a supervisord program, independent of the Hermes version

**C5 — the stall watchdog.** Both witnesses exist at 8.3. It becomes a ninth supervisord program in
the exact shape of `bin/squire-hindsight-healthcheck.py`: two witnesses, strikes with tick-gap
baselining, `supervisorctl restart gateway`, cooldown and per-day caps, maintenance pause, refuse to
act from inside the gateway (here: refuse unless started by supervisord). Alerts go through the
heartbeat to control-api (a `gateway_stalled`/`recovered` field), not e-mail. The shared-helpers
pattern (`_watchdog_common.py`) ports with it. This needs neither the plugin nor Phase ④ and could ship
as a v0.3.x sidecar once ③ is soaked.

**C5 — the recovery page does NOT belong in the container.** Squire already has the pieces: control-api
holds the Railway client and tenant identity; the webhook shim already performs a supervised gateway
restart in the connect flow (`squire-webhook-shim.py` `_handle_llm_connected_post`). A per-tenant
"restart my agent" is a control-api / ingress action authenticated by Squire's own owner binding
(`squire_autopair.py`), not a loopback token page behind an undecided ingress. The personal agent's
open ingress question is answered in Squire by construction.

### Later — when WhatsApp ships for tenants

**C6 — transport external mode** fits Squire better than it fits the VPS: a bridge as its own
supervisord program (priority between hindsight and gateway) with the gateway adopting it over HTTP is
the natural container shape, and it is what stops a gateway restart (or stall recovery) from cycling
the WhatsApp socket. The adopt-only `connect()` fixes from 2026-10-18 (preflight first, lock tolerance,
attach-before-mark, identity + parity refusal) are exactly the code that would run. Scoped mode has
no container equivalent.

**C7 — approvals group→DM mirror.** The ③ plan drops patches 002/003 and makes approvals DM-only
(declared delta 1). Correct for Telegram DMs; revisit the mirror when tenants have groups. `ignored_chats`
and `stale_tool_nudge` are small and portable in ④ if the product wants them; neither is urgent.

### Do not port

- **C8 patches 001–003** (WhatsApp mark-online, GAS read-only scope, DM read receipts): Squire already
  dropped its equivalents (001–004 in ③ S3); the GAS scope returns only with Google tools.
- **C8 patch 004** per-chat tool progress: tenants are DM-only on Telegram, so the managed
  `display.platforms.telegram.tool_progress` key (present at 8.3) does the job without a patch.
- **`login_handoff` / `self_hosted_login`**: inert on the personal agent; no fit.
- **The ops layer as such** (`wa_watchdog.py` cron, pairing-code helper, unit files, `install.sh`,
  `verify.sh`): Squire's equivalents are the supervisord programs and `infra/upgrade_drill.py`.
  What does transfer from `verify.sh` is the *list of post-cutover assertions* → canary-drill
  `--require-*` checks and heartbeat fields (plan 0d/S2 already go this way).

### Process (C9) — transfer, mostly already planned

- The hermes-update skill with a Squire `deploy-adapter.md`: planned for ④ (handoff §Phase ④).
- The review discipline: agent pushes `agent/*` branches, human review before merge, a build guard
  that refuses unreviewed agent commits. Squire's CI (0c) plus branch protection on the org repo
  covers the same need; the guard's idea (block on author identity since the live release) is worth
  one check in `tenant-image.yml` if the agent ever gets a write key there.
- Gotchas worth copying into Squire's skill when it lands: plugin `register()` runs before the
  gateway claims its pid file (never cache a negative "am I the gateway" verdict); upstream session
  keys are `agent:<profile>:<platform>:<chat_type>:…`; owner identities have LID/phone twins (WhatsApp);
  a hand-edited state file is not a fix.

## 4. Suggested order, and what it means for "personal agent as first tenant"

1. **Phase ③ as planned** (v0.2.13 → v0.3.0 → v0.3.1), plus one managed key when safe: disable LSP
   auto-spawn for tenants.
2. **v0.3.x: stall-watchdog sidecar** (no Hermes dependency), with a `gateway_stalled` heartbeat field
   and the restart action exposed through control-api.
3. **Phase ④: base bump**, then guardrails (with home-channel-derived scope) and the isolation seams
   with an rlimit backend, both as `squire_overlay` features behind per-feature arm/disarm like the
   concierge.
4. **WhatsApp launch:** bridge as a supervisord program, transport external mode, approvals mirror.

The gap between the personal agent and a Squire tenant is not the feature list above; it is the
runtime: WhatsApp in-container, guardrails and some workload containment in the plugin, and a watchdog
story that works under supervisord. Steps 2–4 close it in that order.

## 5. Open questions for the founder

- Should tenants be able to turn guardrails notices off (`notify: false`)? On the personal agent the
  keys are locked; for tenants a locked key blocks a legitimate preference.
- Fair-use caps (PRD §5.4): is per-child rlimit containment part of that story, or do caps stay at the
  container level (Railway service limits) only?
- Does "restart my agent" belong in the Telegram concierge (a `/restart`-like command handled by
  control-api) or only in a web surface?
