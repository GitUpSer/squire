# Phase ③ — Squire tenant image to Base + Overlay on Hermes v2026.8.3

## Context

Squire's tenant image carries 6 patches, a 758-line concierge *shell* hook and operator settings
that only reach tenants seeded after each template change. Phase ③ restructures this into the
Base + Overlay model proven on the founder's personal agent — pristine upstream base, then
(tier order) tenant state → managed-scope settings (`/etc/hermes/config.yaml`) → a pip plugin
`squire-overlay` → last-resort patches — **without changing the base** (still
`nousresearch/hermes-agent@sha256:c0cab4e3…` = v2026.8.3 / Hermes 0.20.0 / commit 3c27eb6234).
Behaviour must match v0.2.12 except for the declared deltas below. Phase ④ (base bump via the
hermes-update skill) starts only after ③ is live and stable.

The handoff (`docs/overlay-migration-handoff.md`) came from 9.24 research. Two read+probe rounds
against the real 8.3 tree (critic reconciliation + 5 adversarial verifiers + red team:
21 confirmed / 12 corrected / 0 refuted) changed the recipe:

- Entry point must be **module form** (`squire-overlay = "squire_overlay"`); the handoff's
  `pkg:register` form silently fails at 8.3, and `hermes plugins list` still says "enabled".
- No `ctx.get_config`, `plugin_compat`, approval `request_id`/listing API or bootstrap test
  helpers at 8.3; pytest is not in the image.
- `save_config` strips every managed leaf from the tenant's config on first-DM home adoption
  (verified `hermes_cli/config.py:3535-3545`, `gateway/config.py:466-482`) → any image without
  the same `/etc/hermes` is an unsafe rollback target → minimal managed file + a rollback-floor
  release first.
- Patch 006 is NOT absorbed at 9.24. CI never gated a release on its tests. The drill has no
  overlay signal. Managed scope is a durability tier, not enforcement (tenant `.env` can disable it).

## Founder decisions (2026-09-27)

1. Managed layer minimal in ③: `plugins.enabled [squire-overlay]`, `plugins.disabled []`,
   `telegram.gateway_restart_notification false`, `compression.codex_gpt55_autoraise_notice false`.
   Durability pins (memory.provider, logging.level, telegram streaming, onboarding.profile_build)
   move to v0.3.1.
2. Drop patches 001–004 (001 gated on the fleet audit finding no Anthropic OAuth credential).
3. Patch 005: shutdown hunk → managed key; status-chokepoint hunk stays as a patch; first-contact
   suppression + home adoption move into the plugin, **coupled to the concierge**.
4. Patch 006 stays a patch through ③.
5. Canary: ring 0a existing tenant +1ctest7; ring 0b one fresh tenant walked through onboarding;
   recycle alpha6/alpha8/1ctest9 before rolling; quiet hours; ≥24h soak between releases.

## Release sequence

`Step 0 (prep, no tenant change) → v0.2.13 (rollback floor, first real drill) → v0.3.0 (plugin) → v0.3.1 (durability pins, ④ floor)`

Before each branch: `cd` to the main repo, `git branch --show-current` = main, `git pull --ff-only`
(local main a32c0d7 is behind origin/main 1ada0a7 — only `apps/web/site/` differs). Any merge
touching `apps/control-api/**` auto-deploys control-api **to staging only** (deploy.yml:5-10; prod
needs a manual `workflow_dispatch`). 0a (`tests/test_fleet_audit.py`) and 0e
(`tests/test_upgrade_drill.py`) also live there — merge them before 0d, or after 0d's staging ALTER.
**Founder-run** (with `!` or in the GitHub UI): railway ssh, ALTER TABLE, deploy.yml prod dispatch,
Railway variable changes (`TENANT_IMAGE` — each redeploys control-api; check `/readyz` after),
tag pushes, drills, tenant deletes, the GHCR probe dispatch, GHCR package-access edits / PAT
secrets, and deleting the probe's package version.

### Step 0 — prep (no tenant impact)

**0a Fleet audit** — new `infra/fleet_audit_probe.py` (stdlib, runs in-container via
`railway ssh … /opt/hermes/.venv/bin/python -I -B -c "<zlib+b64>"`, prints one `SQUIRE_AUDIT {json}`
of bools/ints/whitelisted tokens only — never secrets) + `infra/fleet_audit.py` (laptop driver over
`GET /internal/fleet` via `_control_api_client.ControlAPI`, retries railway-ssh metadata blobs,
`--snapshot` mode for rollback diffs, allowlisted `railway variables` names). Records per tenant:
image ref, `_config_version`, home_channel present/== owner, plugins.*, managed-candidate keys,
hooks/allowlist, gateway_state + supervisorctl + s6 Bug-3 signature, concierge state + valve,
**all Anthropic OAuth sources** (`/opt/data/.claude/.credentials.json`, `.anthropic_oauth.json`,
auth.json pool, `.env`/process env), and presence of `HERMES_MANAGED_DIR`/`PYTEST_CURRENT_TEST`/
`HERMES_SAFE_MODE`, plus a derived column **"no home_channel AND no concierge-state"** (v0.1.0-0.1.1
tenants never seeded concierge-state, so plugin adoption can't reach them — handle by hand).
Test: `apps/control-api/tests/test_fleet_audit.py` (fixture tree with planted secrets; assert none
leak). Gates: canary choice, recycle list, and the 001 drop.

**0b GHCR probe** — `.github/workflows/ghcr-probe.yml` (workflow_dispatch, packages:write): push
`:ghcr-probe-<run_id>` from a `FROM scratch` image, assert `:latest` digest unchanged, anonymous
pull of probe + `:0.2.12`. On 403: add GitUpSer/squire with Write under the package's "Manage
Actions access" (fallback: PAT secret). Delete only the probe's own package version — the founder
does this in the UI (deleting a version likely needs package Admin, not the token's Write).

**0c CI hardening** (`.github/workflows/tenant-image.yml`) — add main-push trigger (paths
`tenant-image/**`, push=false); build job `needs: patch-overlay-check`; hard guards replace the
graceful skips (:54-62, :140-148, all `steps.present` ifs); job-level `PATCH_DIR` env replaces the
:259 literal; `compileall -q tenant-image`; build job checkout gets `fetch-depth: 0` (it has none
today, tenant-image.yml:44-45, so there is no `origin/main` to check against); tag builds validate
the version regex, require the commit to be on main, and **refuse to re-push an existing tag**
(`imagetools inspect`); a "suites are wired" check. v0.2.13's overlay contract test runs as a
plain `python3` script (the current CI convention, :314-421); S2 replaces that step with pytest.

**0d control-api heartbeat fields** — `schemas.py` HeartbeatRequest (keep `extra="forbid"`) +
FleetTenant: `overlay_state` (Literal absent/stale/invalid/loaded/degraded/failed/disabled, exported
as `OVERLAY_STATES`), `overlay_features` (bitmask), `overlay_errors`, `managed_ok`, `managed_keys`,
`managed_env_flags` — all optional; `models.py` Heartbeat columns (VARCHAR(16), not a PG ENUM);
map in `routers/internal.py` `fleet()`; `db.schema_drift()` + unauthenticated `GET /readyz` (503 on
missing columns). Tests: test_fleet.py (legacy payload → 200/None, bad values → 422, /readyz),
test_privacy_schema.py EXPECTED_COLUMNS, test_cross_service_contracts.py (ast-parse heartbeat:
always `LEGACY_PAYLOAD_FIELDS ⊆ model_fields`; the overlay checks only when those names exist, so
0d passes before v0.2.13 and v0.2.13 needs no control-api test edit). **Fix the heartbeat's
constant form now:** `LEGACY_PAYLOAD_FIELDS` (16-field literal), `OVERLAY_FIELDS` (6-field literal),
`PAYLOAD_FIELDS = LEGACY | OVERLAY`, `OVERLAY_STATES` tuple; the strict `OVERLAY_STATES` equality
check lives tenant-side in `tests/test_heartbeat.py` (ast-parsing `schemas.py`). Add the heartbeat
and Dockerfile paths to both `on.*.paths` **and** the `dorny/paths-filter` `control_api` filter in
test.yml (:57-61), or the job is skipped.
**Order:** PR green → founder runs `ALTER TABLE heartbeat ADD COLUMN IF NOT EXISTS …` on staging →
verify via information_schema → merge (auto-deploys staging) → staging `/readyz` 200 → founder runs
the same ALTER on prod → verify → founder dispatches deploy.yml `service=control-api` to prod →
prod `/readyz` 200. All before the v0.2.13 tag (otherwise v0.2.13 beats 422 on prod and
`--require-managed` fails the canary).

**0e Drill** (`infra/upgrade_drill.py` + `test_upgrade_drill.py`) — reject `--rollback`+`--canary`;
reject `v`-prefixed tags; `ROLLBACK_FLOOR=(0,2,12)` (→ 0.2.13 after soak) with
`--allow-below-floor`; per-target `--require-managed`/`--require-overlay` (defaults from
`REQUIRES_SINCE`, None = not proven); anonymous GHCR digest resolution in new `infra/_registry.py`
(target becomes the `repo@sha256:` form that `resolve_image_ref` already handles — whether Railway
accepts `repo:tag@sha256:` is untested, so probe it on a staging service before relying on it;
401/404 aborts before touching tenants); one comparator for skip/wait/report; print the
TENANT_IMAGE bump/revert commands; `--include-stopped`. `REQUIRES_SINCE` is the single name for the
per-target requirement defaults (S5 extends it).

### v0.2.13 — rollback floor (= v0.2.12 + /etc/hermes + heartbeat v2)

Patches do **not** move yet; home-template, `HERMES_ACCEPT_HOOKS`, `SQUIRE_SHUTDOWN_NOTICES` and
supervisord unchanged.
- `tenant-image/overlay/overlay.yaml` — flat base pin (image, tag v2026.8.3, digest, commit
  3c27eb6234…, hermes 0.20.0, plugin_key).
- `tenant-image/overlay/managed/config.yaml` — exactly the 4 founder keys (each verified to reach a
  managed-aware reader at 8.3; `plugins.enabled` naming an absent plugin is a no-op).
- `tenant-image/overlay/gate/managed_gate.py` — build gate run **as uid hermes**: clears bypass
  vars, seeds a hostile tenant config, `import hermes_cli.main` (catches .env/dir crash modes),
  asserts managed dir, exact key set, no `/etc/hermes/.env`, not writable, managed values win via
  `load_config_readonly()` and `load_gateway_config()`; emits `managed-manifest.json` (sha256+count).
- `tenant-image/overlay/tests/test_overlay_contract.py` — FROM digest == overlay.yaml; managed leaves
  == founder set == gate EXPECTED; no null/`{}`/`${`/`_config_version`/hooks/skills/model/timezone;
  every leaf identical in home-template (or rollback-harmless new); managed ⊆ frozen `FLOOR_MANAGED`;
  Dockerfile install/COPY lines present; `.dockerignore` globs.
- `Dockerfile` — §3b after :325: `install -d -m 0755 -o root -g root /etc/hermes`,
  `COPY --chown=root:root --chmod=0644 overlay/managed/config.yaml /etc/hermes/config.yaml`;
  §4b after :386: run the gate via `runuser -u hermes` + `stat`/`test ! -w`/`test ! -e .env` checks;
  `ENV SQUIRE_HEARTBEAT_SETTLE_SECONDS=150`; rewrite the :59-69 "no second digest" comment.
- `.dockerignore` — `**/`-anchored `.env*`, keys, `__pycache__`, `*.py[cod]`, `tests/`,
  `.pytest_cache`, `*.egg-info`, `.venv`. (S2 adds `!overlay/tests/` and
  `!overlay/tests/fixtures/` negations after `**/tests/`; the contract test's glob check must allow them.)
- `bin/squire-heartbeat.py` (stdlib) — `LEGACY_PAYLOAD_FIELDS` + 6 new; `managed_state()` (manifest
  hash compare, no YAML parsing); `managed_env_flags()`; `overlay_status()` reader
  (`/opt/squire/run/overlay-status.json`, pid validated via `/proc/<pid>/cmdline`, 16 KiB cap →
  `absent` throughout v0.2.13); **one-shot settle beat** (fires on validated status or at the
  150 s cap; never keyed on `gateway_up`; also rescues a lost first beat); 422 → one legacy resend.
  Tests in `tests/test_heartbeat.py` with a fake clock (exactly one settle beat, zero gateway_up
  calls, 422/500 paths, managed/overlay reader cases).

**v0.2.13 drill (founder-run):**
1. Audit → recycle alpha6/alpha8/1ctest9 (confirm each; irreversible) → top up bots (`load_bots.py`).
2. Ring 0a: snapshot +1ctest7 → `upgrade_drill.py --image-tag 0.2.13 --canary <id>` (managed
   required) → fleet row `managed_ok/keys=4/env_flags=0/overlay=absent`, settle beat ~150 s,
   effective values via `runuser -u hermes -- hermes config get …` (never as root), SIGTERM→
   `stopped: gateway` delta (s6 grace — **if the drain is cut short, ship
   `S6_KILL_GRACETIME`/`S6_SERVICES_GRACETIME` in a 0.2.14 before rolling**), Telegram round-trip.
   **Measure boot-to-adapters-up time** on 0a (and 0b); set `SQUIRE_HEARTBEAT_SETTLE_SECONDS` from it
   (red-team range 120-180 s) so v0.3.0's one-shot beat can't fire before the status file exists.
3. Rollback rehearsal on 0a: `--canary <id> --image-tag 0.2.12` (never `--rollback`), diff snapshot,
   forward again.
4. Ring 0b: provision a fresh tenant, canary it to 0.2.13 before first tap, first DM shows the
   `managed setting(s) were not saved` strip, mid-onboarding canary to 0.2.12 (state/valve intact,
   no shutdown notice), forward, finish onboarding. **Known losses when a stripped tenant rolls
   back to 0.2.12** (not failures): `compression.codex_gpt55_autoraise_notice` reverts to the
   default True (it's a template key, so the strip removes it), and the `/restart` reply and
   planned-restart "Gateway online" broadcast return.
5. Quiet hours: bump `TENANT_IMAGE` to the digest ref (check `/readyz`) → `--roll` → ≥24 h soak
   (awake-hours vs 0.2.12, re-audit) → commit `ROLLBACK_FLOOR=(0,2,13)`. (Bump before the roll so
   new signups don't land on the old image mid-roll; revert it first on any rollback.)

**Shared status contract (fixes a mismatch between the two halves; defined once in 0d, used by
v0.2.13's reader and v0.3.0's writer):** file `state ∈ {loaded, degraded, failed, disabled}`;
heartbeat/control-api `OVERLAY_STATES = {absent, stale, invalid, loaded, degraded, failed, disabled}`;
`overlay_features` bits: 1 = concierge, 2 = first_contact_suppression, 4 = home_adoption.
Writer reports `loaded` only when all three are armed, managed dir is `/etc/hermes`, none of
`HERMES_MANAGED_DIR`/`PYTEST_CURRENT_TEST`/`HERMES_SAFE_MODE` is set, and effective telegram
`gateway_restart_notification` is False. `--require-overlay` ⇒ `overlay_state == loaded` and
`overlay_errors == 0` (pid validated by the heartbeat).

### v0.3.0 — the overlay (branch `phase3/v0.3.0`; every commit builds)

Preconditions: v0.2.13 fleet-wide and soaked; fleet audit written down (the 001 drop is blocked
until it shows no Anthropic OAuth source anywhere).

**Design decisions made in planning (all verified against 8.3):**
- **D1 Coupled suppression seam:** the concierge's own `pre_llm_call` strips upstream's intro
  (and `profile_build_directive()` when "ask") from the *not-yet-consumed*
  `agent._gateway_turn_context_notes`, reached via `agent.subagent_lifecycle.get_active_subagent_parent()`
  (assigned `gateway/run.py:4794-4796`, bound `run_agent.py:7652`, hook fires at
  `agent/turn_context.py:1053` before consume at `:1111`). The strip is the last statement of the
  callback that returns the directive ⇒ **suppression never happens without the directive**, needs
  no gateway class wrapper, and re-registers cleanly under forced rediscovery. (The staging seam
  `_set_pending_turn_sidecar_notes` was rejected: it runs before, and must predict, the inject
  decision; a wrong guess persists in the replayed sidecar.) Exact-element removal at `\n\n`
  boundaries only; skipped when user content isn't a string (native-image turns).
- **D2 Drop `ENV HERMES_ACCEPT_HOOKS=1`:** allowlisted entries register without it
  (`agent/shell_hooks.py:250-280, 680-688`); existing volumes keep running the stub, new volumes
  skip it, rollback targets (0.2.13) still carry the ENV. Narrows agent-added-hook exposure.
- **D3 Mandatory exit-0 stub** at the exact old path `/opt/squire/bin/squire-concierge-hook.py`, in
  every image from 0.3.0 on (consent persists in `shell-hooks-allowlist.json`); no managed
  `hooks: []` (rollback-unsafe); template keeps its hooks block and all duplicated keys.
- **D4 Home adoption drops 005's `not history` gate** (so homeless v0.1.2-era tenants that have a
  concierge-state file adopt on their next owner DM) — declared delta. v0.1.0-0.1.1 tenants never
  seeded `concierge-state.json`, so they are NOT covered; the 0a audit column lists them for manual
  handling.

**S1 Pure patch move (one commit):** `git mv tenant-image/patches tenant-image/overlay/patches`
(exec bits kept); `apply-patches.sh`/`verify-markers.sh` get `${1:?usage}` and
`--no-backup-if-mismatch`; Dockerfile:322 `COPY overlay/patches/ /opt/squire/patches/` (in-image
path unchanged); CI `PATCH_DIR=tenant-image/overlay/patches`; update the 5 test files' paths
(`test_concierge_onboarding.py`, `test_patch_markers.sh`, `test_status_notices_gate.py`,
`test_shutdown_notices_gate.py`, `test_identity_refresh_ttl.py`). Verify: no `*.orig` in `/opt/hermes`.

**S2 Plugin + stub + Dockerfile stages (one commit):**
- `tenant-image/overlay/plugin/` — `pyproject.toml` (exact-pinned setuptools, no deps,
  `[project.entry-points."hermes_agent.plugins"] squire-overlay = "squire_overlay"`); package
  `squire_overlay/` with no hermes imports at import time and plain-`def` hooks only:
  - `__init__.py` — `register(ctx)` wraps everything in `except BaseException`; honours
    `SQUIRE_OVERLAY_DISABLE`; per-feature try/except arms/disarms; suppression armed only if the
    concierge is armed; starts status.
  - `_state.py` (lock-guarded feature flags/counters), `_config.py` (`ctx.get_config` if present
    else lazy `hermes_cli.config.load_config_readonly()`).
  - `concierge_core.py` — **`git mv` of `bin/squire-concierge-hook.py`** (history kept), shebang/
    `main()` removed, lines 134-688 verbatim (directives, state paths, valve read/bump with legacy
    2-field/bare-int tolerance), plus `compute_context(turn_id)` (bumps valve before returning;
    still reads the volume copy of `state-machine.yaml`).
  - `concierge.py` — `pre_llm_call`: skip if `parent_session_id` or platform ∈
    {subagent, cron, curator}; `compute_context`; then `first_contact.strip_upstream_intro(...)` in
    its own try; return `{"context": …}`; any exception → None (no directive, no strip).
  - `first_contact.py` — `INTRO_NOTE` byte copy of `run.py:17191-17193`; strip per D1; counters
    `stripped/skipped/seam_missing/errors`.
  - `home_adoption.py` — `pre_gateway_dispatch` always returns None; gates (cheapest first):
    concierge-state exists, DM, not LOCAL/WEBHOOK, no in-memory home, no `<PLATFORM>_HOME_CHANNEL`
    env, `gateway._is_user_authorized(src)`; adopts via `HomeChannel(..., name="Squire")` +
    `persist_home_channel` + in-memory update (imports inside the hook).
  - `status.py` — sentinel-guarded single daemon thread, only when argv has `gateway run` (or
    from the hook); never imports `gateway.run` (it sets `_HERMES_GATEWAY=1`); writes
    `${SQUIRE_OVERLAY_RUN_DIR:-/opt/squire/run}/overlay-status.json` atomically once
    `_gateway_runner_ref()` has adapters, then on counter change (≤1/10 s) and every 300 s. Schema
    v1 per the shared status contract (pid, short argv, times, managed dir/count, env flags,
    effective restart-notification, per-feature armed + counters). No free text. The writer takes
    its environment through an injectable mapping, so a unit test can assert `loaded` with a clean
    env (in-image tests must set `HERMES_MANAGED_DIR`/`PYTEST_CURRENT_TEST`, so they assert
    `degraded`).
- `bin/squire-concierge-hook.py` — new 3-line exit-0 stub (100755) whose docstring says why it
  must never be deleted.
- `bin/squire-overlay-check.py` — real-discovery gate (build time and canary): as uid hermes with
  bypass vars unset: `import hermes_cli.main`; managed dir `/etc/hermes`; `_plugins["squire-overlay"]`
  enabled with no error; `pre_llm_call` + `pre_gateway_dispatch` registered, none async;
  `gateway.run` not imported; `_HERMES_GATEWAY` unset; managed file not writable. It runs with
  `SQUIRE_OVERLAY_RUN_DIR=$(mktemp -d)` and asserts that dir stays empty (on a live tenant the real
  `/opt/squire/run/overlay-status.json` already exists, so it must not be checked there).
  **`hermes plugins list` is never used as proof.**
- `Dockerfile` — stages `overlay-wheel` (`uv build --no-config --wheel`, assert entry_points.txt)
  → `runtime` (current layers; after the patch layer `uv pip install --no-config --no-deps --offline
  --python /opt/hermes/.venv/bin/python <wheel>`; run the check as uid hermes) → `overlay-test`
  (dry-run asserts only `+pytest +pytest-asyncio +pluggy +iniconfig`, install with `--no-deps`,
  freeze diff == those 4; run `pytest -p no:cacheprovider /opt/squire/overlay-tests` as hermes with
  `HERMES_MANAGED_DIR=/etc/hermes`; touch a sentinel) → `release` (last, default; copies the sentinel
  so every release build runs the in-image tests; no test deps ship). CI asserts every `FROM` is the
  pinned digest or a stage name. Remove `ENV HERMES_ACCEPT_HOOKS=1` (:432-447). `.dockerignore`
  gains `!overlay/tests/` and `!overlay/tests/fixtures/` after `**/tests/`; overlay tests are COPYed
  **only** into `overlay-test` (just `integration/`, `contract/`, `fixtures/`), never into `release`.
- `tenant-image/overlay/tests/` — conftests are **split by where they run**:
  - top-level `conftest.py`: in-image mode (`SQUIRE_OVERLAY_TESTS_IN_IMAGE=1`) makes any skip or
    zero collected a failure, and requires the plugin to import from `/opt/hermes/.venv`.
  - `integration/conftest.py` and `contract/conftest.py` (hermes-dependent, in-image only):
    autouse temp `HERMES_HOME` with the real template, explicit `HERMES_MANAGED_DIR=/etc/hermes`
    **asserted in-test**, bypass vars cleared, managed/config caches invalidated,
    `hermes_cli.plugins._plugin_manager = None`, `shell_hooks.reset_for_tests()`; fixtures
    `loaded_plugins`, `mock_llm` (HTTPServer capture), `make_agent`.
  - `fixtures/squire-concierge-hook.v0.2.12.py` (hash-checked against the tag in CI).
  - `pure/` (no hermes imports; **CI only**, since several tests read repo-only files — Dockerfile,
    `.dockerignore`, `overlay.yaml`, git tags): directive byte-parity vs the v0.2.12 fixture for all 7 states +
    device-flow + O'Brien (each < 10 000 chars); legacy/garbage states → None, no strip; valve
    compatibility both directions; strip boundary matrix; stub inert (1 MB stdin, rc 0, < 2 s);
    status schema round-trips through the heartbeat reader; managed/template contract (moves here
    from v0.2.13's `test_overlay_contract.py`).
  - `contract/test_upstream_surface_83.py` (AST/source-index, line-agnostic): hook names ∈
    VALID_HOOKS; module-form entry point; single caller of `_set_pending_turn_sidecar_notes`;
    `INTRO_NOTE` byte-equal; `pre_llm_call` before `consume_gateway_turn_context_notes`;
    `bind_subagent_parent` around `run_conversation`; notes join assignment; `pre_gateway_dispatch`
    before `_is_user_authorized` under `if not is_internal`; `persist_home_channel`/`HomeChannel`
    shapes; shutdown sends check `gateway_restart_notification`; `invoke_hook` doesn't await;
    Telegram adapter `__module__` prefix `hermes_plugins.telegram_platform`; no `ctx.get_config`.
  - `integration/` (real upstream code): register twice + `force=True` → one hook each, one status
    thread, no wrapper stacking; process scope (cron/mcp/doomed gateway → no status file, no
    `gateway.run`); **coupling on a real AIAgent turn** (armed → wire has directive + reset/voice
    notes, no intro; concierge register raising / build raising / complete / exhausted valve /
    multimodal → intro stays); **legacy-hook neutralization** (pre-seeded allowlist, no ENV, both
    registration sites, one real turn → exactly one "Onboarding is NOT finished"; also fresh volume,
    real v0.2.12 script, ENV=1 variants); a **real `delegate_task` child** and a background-review
    fork get nothing; home adoption with a real `squire_autopair.maybe_bind_owner` pairing file
    (owner adopts once with **exactly one `save_config`** on the loop; stranger/group/existing
    home/env home/no state → nothing); shutdown-notice equivalence via real
    `load_gateway_config` (template, stripped, v0.1.6 template, user `true`; no user file → True pins
    the entrypoint guarantee); managed file modes; kill switch → `disabled`.
- Docker-free retargeting: `test_concierge_onboarding.py` drives the plugin through a small
  `tests/_plugin_concierge_driver.py` so all copy assertions keep running; §8 flips to "ENV not
  baked, hook path is the inert stub".

**S3 Patch reduction (after the audit):** `git rm` 001-004; regenerate
`005-status-notice-chokepoint.patch` from pristine 8.3 `gateway/run.py` with only the `@@ -630`
status hunk (dry-run: no offset, no fuzz); `markers.tsv` — delete the OAuth, 001-004, 005
concierge/home/shutdown rows, keep profile-build/sethome/status/006 rows, add seam anchors
(first-contact intro literal, `agent._gateway_turn_context_notes =`, `"pre_gateway_dispatch",`,
`with bind_subagent_parent(self)`); `git rm tests/test_shutdown_notices_gate.py` + its CI step;
tighten `test_status_notices_gate.py` (one hunk, one file) and `test_concierge_onboarding.py` §9;
remove `ENV SQUIRE_SHUTDOWN_NOTICES=0` (Dockerfile:515-531). Doc fixes: `docs/prd.md:45`,
`state-machine.yaml` real_because (:165-167) + comments, `config.yaml:166-169`, Dockerfile §3
comment, verify-markers FAIL footer. (Flag, out of scope: `prd.md:11/39/151` still promise Claude Max.)

**S4 Template refresh (handoff step ③.3):**
- `overlay/tools/gen_template_history.py` → `overlay/template-history.json`: per file class + all
  historical sha256s from `git tag -l 'tenant-image-v*'` (json-canonical for hindsight config).
  Image-managed: `skills/concierge/SKILL.md`, `state-machine.yaml`, `hindsight/config.json`;
  `SOUL.md` refresh-if-pristine; `config.yaml` never.
- `bin/squire_template_refresh.py` (stdlib): current hash → record; any historical/origin hash →
  back up to `.squire/template-backups/<UTC>/` (never under `skills/`), atomic write; otherwise keep
  + log "locally modified"; prune to 10 backups; per-file try/except.
- `bin/squire-entrypoint.sh` — first-boot detection fix (:156-159): a missing init marker with
  `secrets/.env.enc`, `template-state.json` or `concierge-state.json` present = existing volume
  (restore the marker, never `overwrite`); run the refresh **fail-soft** (`|| log WARNING`) under
  `set -euo pipefail`; a **new** `ensure_config_yaml` function (it doesn't exist today) re-seeds an
  empty/missing `config.yaml` (the gateway skips managed scope without it).
- `Dockerfile` — `COPY overlay/template-history.json /opt/squire/overlay/template-history.json`
  (without it the refresh silently does nothing); add `squire_template_refresh.py` to the
  py_compile list (:375-383); a boot-path test fails if the history file is missing.
- `home-template/config.yaml` — add `_config_version: 33`; rewrite stale comments (:77-85,
  :117-119, :123-164); no key/value changes. `SKILL.md:50` names the plugin.
- Tests: new `tests/test_template_refresh.py` (every historical hash pristine; modified/pristine/
  older matrix; backups outside skills/; config.yaml and .squire untouched); extend
  `tests/test_boot_path.sh` (:255-256 flips: lost marker doesn't re-run first boot; custom SOUL.md
  kept; old SKILL.md refreshed + backed up; fail-soft cases exit 0 with WARNING; empty config.yaml
  re-seeded).

**S5 CI** (the pytest step lands in the S2 commit so every commit builds): replace v0.2.13's
plain-`python3` overlay step with pinned `pip install pytest pyyaml` + `pytest overlay/tests/pure`
(exit 5 = nothing collected = failure); a CI step for `tests/test_template_refresh.py` (else the
"suites are wired" check fails);
`gen_template_history.py --check`; entry-point form check; v0.2.12 fixture hash check;
`compileall` covers `overlay/`; build targets `release` (runs `overlay-test` transitively); drill
target table `"0.3.0": {managed, overlay}`.

**v0.3.0 drill (founder-run):**
1. PR build shows: check passes, pytest N passed / 0 skipped, no `*.orig`, ENV lacks
   `HERMES_ACCEPT_HOOKS`/`SQUIRE_SHUTDOWN_NOTICES`, stub 0755 and inert, wheel 0.3.0 installed.
   Tag, record digest.
2. Ring 0a: `--canary <+1ctest7> --image-tag 0.3.0 --require-overlay` → `overlay_state=loaded`;
   via railway ssh: status file, `squire-overlay-check.py` as hermes, config.yaml sha unchanged,
   template-state/backups as expected, no overlay warnings in `errors.log`, normal reply.
3. Ring 0b: top up bots; point `TENANT_IMAGE` at the 0.3.0 digest for one provision only, then
   revert (two prod control-api variable changes — each redeploys control-api; check `/readyz`
   after each); walk greet→complete. Turn 1: no /help or /sethome prompt, `home_channel.name: Squire`,
   "Note: 4 managed setting(s) were not saved", valve `greet 1 <turn_id>`, hooks block and
   `_config_version: 33` survive. Mid-flow `--canary <id> --image-tag 0.2.13` (never `--rollback`),
   two turns (state kept, valve continues), forward with `--require-overlay`, finish.
4. Rollback rehearsal on 0a to 0.2.13 and forward **before the roll** (ring 0b only proves the
   fresh-tenant path; this proves refresh backups, restored init marker, stub + allowlist). Only
   valve/state/template files may differ.
5. Bump `TENANT_IMAGE` to the 0.3.0 digest (check `/readyz`) → `--roll` in quiet hours.
6. ≥24 h soak; awake-hours vs 0.2.13; status error counters 0.

### v0.3.1 — durability pins (the Phase ④ floor)

- Add `memory.provider: hindsight`, `logging.level: WARNING`,
  `display.platforms.telegram.streaming: true`, `onboarding.profile_build: off` to
  `overlay/managed/config.yaml` (values identical to the template; contract test enforces). Plugin
  `__version__` 0.3.1; gate/contract expected set → 8 leaves.
- Durability test after `save_config(load_config())` through the **gateway reader**
  (`read_raw_config` + `apply_managed_overlay` — `load_config`/`hermes config get` falsely report
  streaming True) and through `hermes_logging` (applies only when the tenant config is non-empty).
- Contract pin on the **main** streaming path (`gateway/run.py:4361-4377`), not the proxy path
  (:23676).
- `overlay.yaml` gains `rollback_floor:`; v0.3.1 **deletes the frozen `FLOOR_MANAGED` set** (it would
  fail on the 4 new keys). CI asserts managed(HEAD) ⊆ managed(floor tag) with equal values; when the
  floor is the release itself, compare against HEAD and require the floor bump in the same diff that
  adds a key (v0.3.1 → `rollback_floor: 0.3.1`). The drill's `ROLLBACK_FLOOR` reads this value (or
  a test pins the two together). The drill refuses lower targets without `--allow-below-floor`,
  which prints the keys at risk and the restore step (re-append the 4 durability keys to
  `/opt/data/config.yaml`). Policy from here: roll forward, not back.
- **v0.3.1 drill (founder-run):** canary with `--require-managed` (8) + `--require-overlay`; induce
  one strip (`/sethome`, sent from the owner's Telegram) on a **disposable ring-0b tenant**, not
  +1ctest7 (a stripped tenant can't safely roll back below 0.3.1); verify via the gateway reader;
  `--roll`; soak.

## Declared behaviour deltas (for the v0.3.0 canary comparison)

1. 003 dropped → `/deny wait until 5:30` no longer hangs; cross-chat `/approve <key>` gone
   (approvals DM-only). 2. 002 dropped (dead code, no change). 3. 001 + both OAuth markers dropped —
   upstream can still reach its OAuth branch if a Claude credential appears on a volume; never
   claim otherwise. 4. 004 dropped (inert). 5. No `.orig` files in `/opt/hermes`.
6. Subagent/cron/curator/background-review turns no longer get onboarding directives or bump the
   valve. 7. Intro suppressed only on turns where the concierge injects (legacy states, exhausted
   valve and image-first turns now show upstream's intro). 8. Home adoption: DM-only, pre-auth with
   its own auth check, not on internal events, no `not history` gate. 9. Shutdown gate moved to the
   managed Telegram key: the Railway `SQUIRE_SHUTDOWN_NOTICES=1` escape hatch is gone; also silences
   the `/restart` reply and the planned-restart "Gateway online" broadcast; Telegram-only (pin per
   platform if a second adapter ships); fails open if the managed layer is missing.
10. Managed keys can't be set; `hermes plugins enable/disable` print success but do nothing.
11. Managed-duplicated keys stripped from tenant files on first save (`plugins: {}` artifact,
   "Note: N managed…" log). 12. `HERMES_ACCEPT_HOOKS` gone (agent-added shell hooks no longer
   auto-accepted; new tenants log a "not allowlisted" WARNING per process start). 13. Forced plugin
   rediscovery no longer drops the concierge (fix). 14. Pristine older concierge files and SOUL.md
   refresh with backups; a lost init marker no longer clobbers config.yaml/SOUL.md.
15. `_config_version: 33` in new templates. 16. Overlay status + heartbeat fields populated.
Unchanged (pre-existing, not deltas): status chokepoint, 006, directive bytes/valve semantics,
oldest-pending approval-button resolution, directive dropped on image turns, `/footer` baking.

## Verification (summary)

- Every PR: static suites gate the build (`needs:`), overlay `pure/` tests, template-history
  check, contract tests; image build runs the managed gate + real-discovery check as uid hermes and
  the in-image `overlay-test` stage (0 skipped, >0 collected).
- Every release: canary with `--require-managed`/`--require-overlay` (None = not proven), fresh-
  tenant ring 0b, canary-only rollback rehearsal (never `--rollback`), ≥24 h soak comparing
  awake-hours, fleet audit before and after.
- Phase ③ is done when v0.3.1 is fleet-wide and soaked, `overlay_state=loaded` and `managed_ok`
  on every tenant, and the patch set is exactly 005 (status) + 006.

## Residual risks

- D1 relies on private attributes (`_gateway_turn_context_notes`, `get_active_subagent_parent`)
  and `_is_user_authorized` — pinned by contract tests; Phase ④ must re-run them at 9.24.
- Forgotten ALTER TABLE → every heartbeat 500s (mitigated by ordering + `/readyz`).
- GHCR 403 from the org repo (0b probes it first).
- Settle beat adds ≤150 s awake per wake (soak measures; per-tenant `…_SETTLE_SECONDS=0` off).
- The template refresh is a new boot-time writer on every tenant (fail-soft; first 0.3.0 boot
  creates backups fleet-wide).
- Ring 0b's `TENANT_IMAGE` window could catch a real signup — do it while signups are quiet.
- `persist_home_channel` blocks the event loop briefly once per tenant (same as 005 today).
