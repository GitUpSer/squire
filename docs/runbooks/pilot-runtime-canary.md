# Pilot runtime canary

This is an image/runtime check before live founder migration or customer signup.
It does not enable pilot admission, provision workers, messaging outbound, or
the WhatsApp socket. Keep the original founder service running. Use the Railway
skill for infrastructure operations and retain resource IDs in a private
operator record. Never store registry credentials or runtime DEKs in this file.

## Verified release and deployment context

Production project `a97a9074-9f2e-495a-ba19-84594c65acbd`, environment
`e921611e-edc6-44f6-b538-185da4bd4fa1`. Control API deployment
`dcf1e98a-30d6-4981-b17d-1175b0389ca8` and control worker deployment
`914d1b24-3a5a-447c-81fc-d1924c8d5832` reached SUCCESS on 2026-11-22 with the verified rc12 image settings.
Both have the founder image and backup-bucket setting; admission and workers
were read back disabled by the orchestrator. Private registry pull credentials are installed in both control services and
canary registry configuration, never tenant environment variables. The table
below lists the rc12 images accepted after both deployed canaries passed startup
and restart checks. This is image acceptance; model and live WhatsApp delivery
remain separate gates.

Both images use repository `ghcr.io/gitupser/squire/hermes-tenant-pilot`:

| Variant | Immutable digest | Memory packages |
| --- | --- | --- |
| Standard | `sha256:36f7a613c32c883faf0a2ac46de995999cdf10a4dec160d7b83b5edde24a67c2` | Hindsight 0.9.0 / pg0 0.15.1 |
| Founder | `sha256:89ee116638a88278b938f157c109d39b8783107210842c3973b224f1413a29d2` | Hindsight 0.5.2 / pg0 0.12.3 |

The founder backup bucket is `pilot-founder-dm-backups`, ID
`6aa8afe8-e842-41ce-a9ee-2f33d331ed8f`. It is reserved for the actual founder
private runtime; a disposable canary must not receive its credentials. The
shared operations bucket credentials must never enter either runtime.

The unpatched pinned upstream `container_boot.main()` reconciles persisted
gateway state before Squire's CMD. An actual-source dry-run confirmed that
`gateway_state=running` or `desired_state=running` starts an s6 gateway even with
`HERMES_GATEWAY_NO_SUPERVISE=1` and `SQUIRE_RUNTIME_ID` set. An empty cold canary
is safe for initial inspection, but production promotion/restart acceptance
requires a Squire-only guard preventing that second supervision owner. Patch
`008-single-gateway-owner.patch` now implements that guard, with an actual-source
behavior gate that fails on the original source and preserves legacy behavior.
The rc12 images above contain this guard; standard deployed restart verification
passed with persisted running intent and exactly one gateway. Merely
passing `--no-supervise` to the later supervisord gateway does not prevent the
earlier reconciliation. Record the replacement immutable image if this guard
changes the release. The probe rejects extra gateways and a supposedly fenced
canary containing any gateway process.

## Standard cold-start canary

1. Create a dedicated service such as `pilot-runtime-canary-standard` and a
   dedicated volume at `/opt/data`. Do not insert a `PilotAccount`,
   `PilotAdmission`, messaging runtime, phone binding, or founder import record.
   Configure the exact standard digest and private registry pull credentials
   through Railway's registry-credential field. Never use a public package-policy
   change or expose the pull credential as a tenant environment variable.
2. Give the service one replica, no sleep, no deployment overlap, a generous
   cold-start health deadline (at least 600 seconds), and `/health` on port 8080.
   Use no public domain or TCP proxy. Start with a 5 GiB private volume and 4 GiB
   RAM / 2 CPU for this diagnostic; measure steady use before choosing customer
   limits. Shutdown drainage must cover all supervised programs, including the
   browser's 15-second stop ceiling in addition to the earlier 165-second total.
3. Seed the following variables before the authoritative deployment. Generate
   three distinct random capabilities of at least 32 characters and one fresh
   32-byte base64 DEK in a private operator process. Use opaque canary IDs, never
   the real founder or tester identity.

   ```text
   TENANT_ID=pilot-canary-standard-<opaque-id>
   SQUIRE_RUNTIME_ID=<same-id>
   SQUIRE_OWNER_TENANT_ID=<same-id>
   SQUIRE_CONVERSATION_ID=canary-<opaque-id>
   SQUIRE_RUNTIME_KIND=dm
   SQUIRE_DEPLOYMENT_GENERATION=1
   SQUIRE_IMAGE_REF=<standard-repository@digest-above>
   SQUIRE_VOLUME=/opt/data
   HOME=/opt/data
   HERMES_HOME=/opt/data
   PORT=8080
   SQUIRE_MESSAGING_URL=http://127.0.0.1:9
   CONTROL_API_URL=http://127.0.0.1:9
   SQUIRE_INGRESS_TOKEN=<unique-canary-only-token>
   SQUIRE_EGRESS_TOKEN=<different-canary-only-token>
   SQUIRE_CONTROL_TOKEN=<different-canary-only-token>
   SQUIRE_DEK=<fresh-base64-32-byte-key>
   ANTHROPIC_API_KEY=
   SQUIRE_PRESERVE_LOCAL_EMBEDDINGS=true
   HINDSIGHT_API_EMBEDDINGS_PROVIDER=local
   HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=BAAI/bge-small-en-v1.5
   HINDSIGHT_API_RERANKER_PROVIDER=rrf
   HINDSIGHT_API_LLM_PROVIDER=none
   HINDSIGHT_API_SKIP_LLM_VERIFICATION=true
   HINDSIGHT_API_WORKER_ENABLED=false
   ```

   Do not seed any WhatsApp/Telegram session credentials, private owner keys,
   production service capability, `RESTIC_REPOSITORY`, or bucket credentials.
   Loopback port 9 must remain unbound; it is an intentionally unavailable broker
   destination. The empty runtime has no events or scheduled jobs to execute.
4. Wait for image download, real local embedding initialization, embedded
   PostgreSQL, browser, and gateway startup. `/health` alone is insufficient: the
   shim intentionally answers 200 before the adapter is ready.
5. Copy `infra/pilot_runtime_canary.py` to a temporary path in this canary, then
   execute with the Hermes interpreter and the runtime home environment:

   ```sh
   HOME=/opt/data HERMES_HOME=/opt/data SQUIRE_VOLUME=/opt/data \
     /opt/hermes/.venv/bin/python /tmp/pilot_runtime_canary.py \
       --variant standard --expected-image 'ghcr.io/gitupser/squire/hermes-tenant-pilot@sha256:36f7a613c32c883faf0a2ac46de995999cdf10a4dec160d7b83b5edde24a67c2'
   ```

   The probe only reads local metadata and health endpoints. It emits no tokens,
   messages, memory contents, account identifiers, or provider credentials. A
   passing cold canary has a live adapter, correct package versions, a mounted
   home, tmpfs secret links, running core services, and zero inbox/outbox/tool
   activity. `model_configured=false` is expected without a funded canary key;
   this check does not claim a successful model request.
6. Once a valid service-funded model credential exists, create a short-lived,
   tightly budgeted canary proxy key. An actual Hermes turn requires a separate
   local broker sink fixture or an explicitly isolated broker/control test scope.
   Do not send an `/events` request with a production customer identity or accept
   a successful direct proxy request as full runtime turn coverage. Revoke the
   temporary model key after testing. Test restart/durable-state preservation on
   this canary before accepting the image for customer provisioning.

Creating an image-backed Railway service may initiate an incomplete initial
deployment before its volume/configuration is attached. Keep the canary's unique
scope and unreachable broker endpoints in its initial variables; the later
explicit deployment with the volume is authoritative. An initial refusal to boot
without its required volume is expected, not permission to bypass the volume gate.

## Restored founder canary

Use a second dedicated service and volume, the founder digest, and at least
enough storage for the verified home, embedded PostgreSQL, local embedding cache,
and restore scratch space. A 20 GiB diagnostic volume leaves room for the current
rehearsal; measure actual use before final sizing. Never mount or change the source
agent's filesystem from this service.

- Start the container in a staging mode that cannot launch the gateway, cron,
  memory API/worker, browser, or credential refresh before the reviewed home and
  restored database are present. Verify the actual image entrypoint semantics;
  do not assume a custom Railway start command bypasses upstream s6 bootstrap.
- Stage only the positive file list produced by `pilot_founder_transform.py`.
  For the pre-cutover clone, omit owner OAuth/auth.json and active integration
  credentials. Even an idle adapter's provider-readiness resolver can refresh
  OAuth; a second refresher must not race the still-live source. Use no model
  provider or a distinct canary-only proxy key instead.
- Restore the logical dump into a fresh, explicitly named database in this
  canary's own embedded PostgreSQL. Discover the actual instance configuration
  without logging its password. Pin the exact canary service ID and runtime ID
  in the restore helper, assert the destination does not exist, and verify
  PostgreSQL 18/extensions and the source dump checksum before creating it.
  Coordinate its PostgreSQL lifecycle and explicit API connection with the
  installed pg0 version; do not overwrite the automatically seeded database.
- Existing `pilot_restore_drill.py` and `pilot_hindsight_pending.py` are pinned
  to the already-deleted scratch service `05391445-8c9a-4cda-a823-73f1468891e6`.
  They are rehearsal evidence, not executable founder-canary restore commands.
  Prepare a separately scoped helper once the new canary resource ID and embedded
  database connection are known; do not weaken or spoof the old service guard.
- Keep `HINDSIGHT_API_WORKER_ENABLED=false`, gateway and scheduler fenced during
  restoration, and use real local BGE-small embeddings with RRF. Compare all 15
  table inventories, the original bank identity, 14,324 memory records, 1,132
  documents, 5,228 entities, source content hashes, and terminal queue counts.
  The matching schema head is `h3i4j5k6l7m8`; Hindsight 0.9 is not a valid founder
  upgrade. Then perform owner-approved, read-only recall checks without retaining
  or consolidating any data. Do not print returned memory text into CI/logs.
- The remapped history rehearsal has 38 verified founder DM sessions; 168
  unidentified sessions remain in the encrypted archive. Use the actual reviewed
  target conversation identity for the final migration; the existing standalone
  remap proof used a synthetic target and is not a production identity mapping.
- First adoption needs the checked `.squire/restored-home.json`, matching image
  and runtime identity, `SQUIRE_RESTORED_HOME=true`, and
  `HERMES_SKIP_CONFIG_MIGRATION=1`. Preserve the image's founder variant. The
  normal entrypoint validates critical hashes before seeding and records normal
  initialization. Do not create `.squire/initialized` by hand to bypass it.
- For native startup, clear the temporary Railway start-command override and
  use the image's default ENTRYPOINT and CMD. Setting only
  `/opt/squire/bin/squire-entrypoint.sh` as a Railway override bypasses upstream
  user setup and creates root-owned credential files that the gateway cannot
  read. Verify the effective command is empty and inspect actual process/file
  ownership after startup. In this rehearsal, `startCommand: null` did not clear
  the field; an empty string did. A fresh `serviceInstanceDeploy` applied the
  changed configuration; redeploying the previous build reused its old `sleep`
  startup snapshot. Platform SUCCESS alone is not runtime acceptance.
- For Hindsight 0.5.2 native lifecycle, `pg0://<instance>:<port>` starts the
  named instance with upstream's `hindsight` database/user/password defaults.
  Restore into a separately prepared cluster seeded with `postgres`, then create
  a fresh `hindsight` database for the verified dump. Do not repoint native pg0
  at a reference cluster with a different user/database or overwrite a seeded
  database. The scoped `pilot_founder_canary.py` helper preserves the reference
  cluster and verifies clean managed shutdown plus complete data fingerprints
  before native handoff. Stale process metadata may be archived only after
  checking the exact path, dead/zombie PID, absent postmaster.pid, and clean
  PostgreSQL control state. Never remove live database files to clear a lock.
- If booting a prepared canary while keeping the gateway fenced, use a reviewed
  temporary supervisord configuration with gateway autostart disabled. The
  metadata probe's `--gateway-fenced` flag skips adapter/gateway readiness; it
  does not itself fence a process. Other core services and scope configuration
  still must satisfy the probe. Do not confuse this with full chat continuity.

The actual owner credentials, final snapshot, founder operator import, source
writer fence, transport cutover, group tests, rollback drill, and soak remain
separate live gates. `/v1/operator/founder` consumes an admission seat and queues
provisioning; it must not be used merely to reserve a throwaway canary.

## Tester invitation

The consenting second tester's email is in the private operator conversation.
Keep it out of repository files and logs. The route
`POST /v1/operator/invitations` with body `{"email":"<consenting tester>"}` is
internal-token protected and returns a secret `signup_url` plus an expiry 14
days later. It does not send an email or consume a seat by itself. Create one
invitation and store the returned link privately; avoid generating duplicates.

Wait until the live sender, funded models, runtime provisioning and admission
are ready before delivering the invitation. The tester should enter their phone
on the website and receive the first WhatsApp challenge through that flow; no
preliminary WhatsApp message should be sent to their number. Preserve proof of
the subsequent first agent reply separately from the earlier synthetic browser
acceptance result.
