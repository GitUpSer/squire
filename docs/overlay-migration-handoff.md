# Handoff: move Squire to Base + Overlay, then update Hermes

Written 2026-09-27 at the end of the session that migrated the founder's personal agent. This
file is the starting context for **Phase ③** (restructure Squire) and **Phase ④** (update
Squire's base). Order is user-mandated: ③ must be live and stable before ④ starts.

## What was proven on the personal agent (sandbox)

Repo: `github.com/shagarwal/hermes-agent-bootstrap` (private), local at
`/mnt/c/Users/shaur/Documents/Code_Repository/hermes-agent-bootstrap`. Read these first:

| File | Why |
|---|---|
| `docs/architecture.md` | The model: pristine upstream base + plugin-first overlay, tier order for changes. |
| `.claude/skills/hermes-update/` | The update skill (TDD-tested). Copy it here in ④; **only `deploy-adapter.md` changes**. |
| `overlay/` | Reference implementation: managed layer, plugin, patches, contract tests. |
| `docs/update-log.md` | What happened to each old customization, v2026.8.3 → v2026.9.24. |
| `docs/reference/hermes-plugin-api-v2026.9.24.md` | **Plugin API research with file:line citations** covering hooks and whether their returns are honored, middleware kinds and coverage, the approval flow internals, `register_command`, `plugin_compat`, skills loading, config keys, and the CLI. The most useful single reference for ③. |
| `docs/reference/recon-v2026.9.24.md` | Intent and absorption analysis per customization, plus config migrations 30→46. |
| `overlay/tests/conftest.py`, `overlay/tests/contract/`, `overlay/tests/unit/` | The test-harness pattern to copy (see below). |

Result: 5 commits + 6 patches became 1 plugin + 1 managed-settings file + 3 one-line patches.
The update is live on v2026.9.24 (Hermes v0.21.5), and 20 overlay tests run against upstream's
real code.

## Architecture decisions (user-approved, apply to Squire too)

- **Base = the upstream image pinned by digest, never edited.** Squire already does this
  (`tenant-image/Dockerfile:53`).
- **Overlay attaches plugin-first:** a pip package with entry point in `hermes_agent.plugins`,
  which must also be listed in `plugins.enabled`. Patches are the last resort, capped, and
  fail-closed on markers. Squire's `apply-patches.sh` and `verify-markers.sh` are already the
  right tooling. Port the `--no-backup-if-mismatch` flag from the bootstrap copy.
- **Settings tier = Hermes "managed scope"** (`hermes_cli/managed_scope.py`). A `config.yaml` in
  `/etc/hermes` (or `$HERMES_MANAGED_DIR`) deep-merges ON TOP of the user config at read time.
  Migrations can't override it, and tenants can't change those keys (they're locked).
  **This is ideal for Squire:** bake it into the image, and every tenant gets operator-pinned
  settings that survive updates. Keep it minimal, since locked keys also block tenants.
- **Contract tests are the compatibility gate.** Pip plugins get no `requires_hermes` check.

## Target architecture for Squire (self-contained spec)

### The layers

```
BASE     docker.io/nousresearch/hermes-agent@sha256:<digest>      FROM, never edited
         (upstream installs itself editable at /opt/hermes, venv sealed)
   │  attaches only through: managed scope · plugin entry point · ≤N patches
OVERLAY  tenant-image/overlay/                                     (new; everything Squire adds to Hermes)
   overlay.yaml         base pin: tag, image digest, commit, hermes version (ONE place)
   managed/config.yaml  → COPY to /etc/hermes/config.yaml          operator-locked tenant settings
   managed/.env         → COPY to /etc/hermes/.env (optional)      operator-locked env (e.g. SQUIRE_* knobs)
   plugin/              pip package squire-overlay → installed into the /opt/hermes venv
   patches/             last-resort diffs + markers.tsv (existing tooling moves here)
   tests/contract/      the upstream surface we depend on
   tests/unit/          features exercised through upstream's real code paths
NOT OVERLAY (already separate, unchanged by ③):
   home-template/       tenant-owned DEFAULTS (SOUL.md, skills, config) seeded into the volume
   bin/, supervisord    Squire sidecars (webhook shim, secrets, heartbeat, backup, hindsight)
   /opt/squire/venv     Squire's own Python env
RELEASE  image tag tenant-image-vX.Y.Z = base digest + overlay sha; immutable; rollback = previous tag
```

### How the personal (VPS) model maps to Squire

| Concept | Personal VPS | Squire |
|---|---|---|
| Base pin | `overlay/overlay.yaml` tag + commit | same file + **image digest** (the Dockerfile `FROM` reads it, or CI checks they match) |
| Release artifact | `~/.hermes/releases/<tag>__<sha>/` worktree + venv | image tag |
| Build gates | `deploy/build-release.sh` | Dockerfile steps + `tenant-image.yml` CI |
| Smoke | `deploy/smoke.sh` on a throwaway HERMES_HOME | a synthetic or ring-0 tenant |
| Cutover | symlink swap (`cutover.sh`) | `upgrade_drill.py --canary` → `--roll` |
| Rollback | `rollback.sh` (symlink back + state restore) | redeploy previous tag (+ volume restore if a migration ran forward) |
| State backup | `~/.hermes/state-backups/<ts>/` | per-tenant volume / B2 backup, which must actually work first |
| Settings tier | `/etc/hermes` → symlink to the release's managed dir | `/etc/hermes/` baked into the image |

### Tier order for every Squire customization (stop at the first that fits)

1. **Tenant state** (the volume): anything a tenant or their agent may change. Seeded from
   `home-template/` and never overwritten. Fixing the seeding refresh is step ③.3.
2. **Managed setting** (`overlay/managed/`): anything the OPERATOR must pin for every tenant and
   keep across updates. Locked from tenants, so keep it minimal.
3. **Plugin, public API** (`overlay/plugin/`): hooks, commands, middleware. An internal import is
   allowed only if `plugin_compat` doesn't flag it and a contract test pins it. Never set
   `plugins.allow_deprecated_imports`.
4. **Patch** (`overlay/patches/`): one concern, one file, a post-apply marker, and a written
   retire condition.

Before adding any item, grep upstream to see whether it already does it.

### Managed scope mechanics (verified at v2026.9.24; re-verify at 8.3)

- **Location:** `/etc/hermes/{config.yaml,.env}`, or `$HERMES_MANAGED_DIR`. The docs recommend
  root-owned files so the tenant process can't edit them.
- **Merge rules:** leaf-level deep merge ON TOP of the user config at read time. Sibling keys stay
  user-controlled. **Lists replace**, so `plugins.enabled` in the managed file is the whole list.
- **Locking:** `hermes config set <managed key>` hard-rejects. Bulk saves strip managed keys with
  a note. `hermes doctor` and `config show` display them.
- **Migrations** rewrite only the user `config.yaml`, never the managed layer. So a managed value
  survives any upstream default change.
- **Plugin settings:** `plugins.entries.<plugin-key>.settings.<k>`, read in the plugin via
  `ctx.get_config("<k>")`. `ctx.get_config` goes through `load_config_readonly`, which includes
  the managed layer.

### Plugin packaging (the exact pattern that works)

- **Entry point:** in `pyproject.toml`, `[project.entry-points."hermes_agent.plugins"]` with
  `squire-overlay = "squire_overlay:register"`. The entry-point name is the key that must appear
  in `plugins.enabled`, and it must be enabled even though it's pip-installed.
- **No plugin.yaml is read for pip plugins**, so there is no `requires_hermes` gate. The
  contract tests are the gate.
- **Structure:** `register(ctx)` calls one `module.register(ctx)` per feature, so removing a
  feature is a one-line change. It logs `squire-overlay <version> loaded`, which is what a health
  check greps for.
- **Useful `ctx` methods:**
  - `register_hook`, `register_command(name, handler, description, args_hint)`
    (built-in names are rejected), `register_middleware(kind, cb)`, `get_config`.
  - Hooks that act on their return value include:
    - `pre_gateway_dispatch`: skip, rewrite or allow. It runs before auth.
    - `pre_llm_call`: injects context.
    - `pre_tool_call`: block, modify or approve.
    - `transform_*`: replace text.
  - The full list is in the API reference, §A1.
- **Middleware coverage:** `llm_request` middleware sees the final provider payload for main
  turns only. It misses aux calls and the iteration summary.
- **Approval hooks:**
  - A plugin approval transport REPLACES in-chat prompts.
  - `pre_approval_request` fires after enqueue, so the `request_id` can be read from
    `list_gateway_approvals`.
  - Resolve with `tools.approval.resolve_gateway_approval(session_key, choice, request_id=)`.
  - Don't use `register_gateway_settle`.

### Test harness pattern (copy from the bootstrap repo's `overlay/tests/`)

- **`conftest.py`** (autouse): a temp `HERMES_HOME`, and `HERMES_MANAGED_DIR` pointed at
  `overlay/managed`. An explicit `HERMES_MANAGED_DIR` is honored under pytest; `/etc/hermes` is
  not. It invalidates the managed-scope and config caches.
- **`loaded_plugins` fixture:** `hermes_cli.plugins._reset_plugin_managers_for_tests()` +
  `discover_plugins(force=True)`. This is REAL discovery, so it proves the entry point, the
  `plugins.enabled` gating through the managed layer, and the compat checks.
- **Contract tests:**
  - hook names are in `VALID_HOOKS`;
  - our command names are not built-ins (`resolve_command(name) is None`);
  - function signatures;
  - ordering assumptions checked with `inspect.getsource` index comparisons;
  - `plugin_compat.scan_plugin(<pkg dir>) == []`.
- **Unit tests** drive upstream's real code:
  - the approval queue: block in `_await_gateway_decision` on a thread, then resolve through
    the plugin command;
  - the dispatcher: `object.__new__(GatewayInboundMixin)._hm_pre_gateway_dispatch_hook(...)`.
- **Threads:** put a `_spawn(fn, *args)` seam in plugin code for off-thread work, and patch the
  seam in tests. Never patch `threading.Thread` globally; that deadlocks the test's own worker.
- **In CI**, run inside the built image so the tests see the exact base:
  `docker run --rm <image> /opt/hermes/.venv/bin/python -m pytest /opt/squire/overlay/tests`
  (adjust the paths to the image's venv).

## ⚠ Squire is on v2026.8.3 — check the plugin surface THERE first

Phase ③ keeps the base version fixed, so every mechanism has to exist **at v2026.8.3**. The
personal work was verified at v2026.9.24. Before designing, grep the 8.3 tree (the image's
`/opt/hermes`, or `git show v2026.8.3:<path>`) for:

- `hermes_cli/managed_scope.py`: does managed scope exist at 8.3?
- `register_middleware`, `VALID_HOOKS` (`pre_gateway_dispatch`, `pre_llm_call`,
  `pre_approval_request`).
- `hermes_cli/plugin_compat.py`: it was new after 8.3, so there's likely no compat disabling
  at 8.3.

If a mechanism is missing at 8.3, that item stays a patch through ③ and moves in ④.

## Squire's current customizations → likely target (verify each)

| Now (`tenant-image/patches/`) | Likely target |
|---|---|
| 001 OAuth system-prompt scrubber | **Probably drop.** Claude-subscription OAuth was dropped for Squire (ToS), so check whether any tenant path still uses Anthropic OAuth. On v2026.9.24, upstream's own transform passed the billing canary without it, so it drops in ④ anyway. |
| 002/003 cross-chat approvals | Needed by tenants at all? If yes, port `approvals.py` from the bootstrap plugin. |
| 004 DM read receipt (WhatsApp, inert in image) | Drop from the Telegram-only image, or keep until PR #79122 merges. |
| 005 first-contact concierge note (214 lines, 4 concerns in `gateway/run.py`) | Split: shutdown notices → `gateway_restart_notification` config (at 9.24; check 8.3); concierge / status gating → `pre_gateway_dispatch` / `pre_llm_call` hooks. |
| 006 Telegram identity TTL | Keep through ③. Absorbed upstream by v2026.9.24, so drop it in ④. |
| `pre_llm_call` concierge hook (shell hook in `home-template/config.yaml`) | Could become a proper plugin hook. |

## Phase ③ steps (base unchanged)

1. Check the 8.3 surface (above), then design `tenant-image/overlay/`: `squire-overlay` plugin,
   `managed/config.yaml`, `patches/`, and `tests/contract/`.
2. Dockerfile: pip-install the plugin into the upstream venv, and `COPY` the managed file to
   `/etc/hermes/config.yaml`. Keep the existing patch apply and marker verify.
3. Fix the **template-version gap**: existing tenants never get new template files, because
   `seed_template` keeps whatever is already there (memory `home-template-seeding.md`). Add a
   baked `template-version` marker; image-managed files refresh on boot, and agent-owned files
   are left alone. The managed layer already removes the need for many template edits.
4. CI: run the contract and unit tests in `tenant-image.yml`.
5. Release `tenant-image-v0.3.0`. **First real run of the canary drill**
   (`infra/upgrade_drill.py --canary`) on ring 0, then `--roll`, then a forced `--rollback`
   rehearsal. Behavior must be identical to v0.2.12.

## Phase ④ steps

1. Copy `.claude/skills/hermes-update/` into this repo. Rewrite only `deploy-adapter.md`:
   digest bump → CI build → canary ring via `upgrade_drill.py` → staged rings; rollback =
   redeploy the previous image tag.
2. Run the skill against the latest tag. Confirm tenant state backups actually work first:
   B2 backups are a no-op until a B2 account exists. A forward config/state migration needs
   a restore path.

## Hard-won gotchas that transfer (full list: the skill's `gotchas.md`)

- Upstream migrations silently change values; the managed layer is the fix.
- A plugin approval transport REPLACES in-chat prompts. `llm_request` middleware misses aux
  calls. Don't use `register_gateway_settle`.
- `plugin_compat` (at 9.24+) silently disables the WHOLE plugin on any flagged import.
- `cmd | grep -q` under pipefail fails spuriously. `pkill -f` can kill its own shell.
- Never copy or restore `auth.json` (Codex refresh tokens rotate).

## Decisions the user owns (ask, don't assume)

- Which settings go in the tenant managed layer. They're locked for tenants.
- Whether tenants need cross-chat approvals at all.
- Canary ring membership and the pace of the roll.
