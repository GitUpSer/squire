# WhatsApp pilot implementation record

This is an execution record, not a claim that the pilot is live. Implementation
is in progress. Public admission and outbound messaging must remain disabled
until the acceptance gates in `wacli-pilot-implementation-plan.md` pass.

## Verified environment (2026-11-15)

- Production project: `squire-prod`, `a97a9074-9f2e-495a-ba19-84594c65acbd`.
- Production environment: `e921611e-edc6-44f6-b538-185da4bd4fa1`.
- Frontend service: `app-web`, `db10d96a-e7f4-4255-ac94-4c03b0590644`.
- Custom domain: `app.thesquire.co`; DNS and ownership verified, TLS valid.
- Transport is healthy in phone-pairing mode; no founder cutover or WhatsApp send has occurred.
- Existing marketing site and staging fleet remain separate.

Required frontend DNS records:

| Type | Host | Value |
| --- | --- | --- |
| CNAME | `app` | `hrl42zw1.up.railway.app` |
| TXT | `_railway-verify.app` | `railway-verify=edb382f2e78286a4745df55840484691a084b7da515fcc54aa9aae62a7bc2ac0` |

## Production resources

| Service | ID | State at last verification |
| --- | --- | --- |
| control-db | `95df0541-8270-4e39-a84e-91e5942c8400` | Database deployment SUCCESS |
| messaging-db | `ebf57ca6-0e7c-426d-98fd-2e38cdb42b8d` | Database deployment SUCCESS |
| litellm-db | `b9520966-65e9-4931-a98b-42c65758de7a` | Database deployment SUCCESS |
| control-api | `e6f51482-3db5-423f-8d76-3a1db48c0641` | SUCCESS; admission closed; follow-up fixes awaiting redeploy |
| control-worker | `c5e4b1e8-4fa6-47e8-a6f7-ad1bdfb4ce3e` | SUCCESS; processing disabled |
| messaging-api | `e2471ffb-9747-4e48-8359-9f656a6e3d3c` | SUCCESS; explicit pre-deploy migration |
| messaging-worker | `bdf3a124-3bc3-4c80-befd-fd1f171f9dff` | SUCCESS; outbound disabled |
| trial-proxy | `40a0c990-bb27-4eb4-a46d-e35b7bf384ad` | SUCCESS; replacement Anthropic credential required |
| wacli-transport | `880eca27-7b70-4b62-adf8-e84e465e2bf3` | SUCCESS; pairing mode; outbound disabled |
| app-web | `db10d96a-e7f4-4255-ac94-4c03b0590644` | SUCCESS; HTTPS 200; operator API paths 404 |

Verified deployment IDs: control API `a4ce6351-a8e5-4d04-92eb-9e4be6eed836`,
control worker `e70dec68-6bf1-4de3-84b0-a26f5deb6fbd`, messaging API
`201e2828-7459-4b3f-a31f-847e64940d3c`, messaging worker
`46e37af0-5530-4df8-b385-bdc2d573109b`, proxy
`e20cfe49-5078-4c99-948e-44ca1f91fd09`, frontend
`0077bde3-28af-40af-a6c0-740148c3e4e4`. These are initial disabled deployments;
subsequent review fixes must be redeployed before activation.

Transport volume `588bc84f-7697-4466-84ad-0e3a671aa28c`: 5120 MB at `/data`,
us-west2, one replica, no sleeping, no deployment overlap. Current healthy
transport deployment: `5136e9e0-7e40-4ebf-943f-244c3550f47b`; source manifest SHA256
`15cc06a37fbd0f182cb699d9a8ac75d9848a12423ae61c77e18a0f4852957846`.
SSH confirmed IPv4 health and volume ownership (UID 10001). Pairing mode keeps
the private health server running while disabling sync and all send mutations.
The first build failed because an IPv6-only listener could not accept Railway's
IPv4 health probes. The explicit dual-stack listener passed both address families.

## Account setup and human-dependent gates

- Founder confirmed access to the primary WhatsApp phone for linking a device.
- Reserve a consenting second tester who has never contacted the sender; do not
  send a preliminary message to that number before the website acceptance test.
- Resend `auth.thesquire.co` is verified (domain
  `a0061ac1-8814-4969-8fa9-4dea47aa971d`). Sender:
  `Squire <hello@auth.thesquire.co>`. Open/click tracking disabled; TLS enforced.
- A separate sending-only key restricted to that domain is installed in control
  service variables. Setup credentials and generated secrets stay in Git-ignored
  local files; no credentials are recorded here.
- Founder supplied a new workspace-scoped Railway provisioning token. Its
  production authorization check passed and it is installed in both control
  services. Workers remain disabled until the runtime release is verified.
- Actual signup email endpoint returned 200 using the restricted production
  Resend key; Resend reported `delivered` for email
  `01a1e0d6-c73f-772e-acb7-3b75f564f2df`.
- A tiny production model request reached Anthropic but returned 401
  `authentication_error: invalid x-api-key`. Its temporary virtual key was
  revoked successfully. A replacement service-funded key was requested in
  `.env.pilot-setup`; never substitute the founder's private model credentials.

## Founder migration inventory

- Live gateway remains active on its existing host, release
  `v2026.9.24__be3f3d797`.
- Conversation SQLite database is approximately 1.4 GB; use SQLite's consistent
  backup mechanism, not a live raw copy.
- Hindsight also has PostgreSQL data outside `.hermes`, under `.pg0` (about
  528 MB including installation and instances). PostgreSQL tools are version
  18.1.0. Identify the active instance and take a logical database backup.
- `hermes-stall-recovery.service` and `hindsight-healthcheck.timer` are active;
  cutover must prevent an old watchdog from restarting the old writer.
- Source disk has limited free space. Avoid accumulating unbounded snapshots.
- No live session credentials have been converted or moved.

## Provider capabilities established

- Pin wacli v0.26.0, commit
  `535a8b969cc83c3aad57f66be77e8fd4a7fcaaa3`.
- Linux amd64 release archive SHA256:
  `2c28f65e2f9aaf32585288229db1f916c6c5fb5db9513715017d528513604b44`.
- Journal/checkpoint commands run against the actual ARM64 binary locally.
- Production uses `0.26.0-squire.1`: the exact upstream commit plus a checked
  patch that stores live/history provenance atomically. Unpatched/unknown origin
  cannot execute agent tasks. The patch SHA256 and original release hashes are
  recorded in `apps/whatsapp-transport/wacli-release.json`.
- Native incoming mention metadata is absent in this release. Initial group
  triggers use `!squire` and replies; no inferred display-name mentions.
- Pairing, fresh-recipient delivery, and live group round trips remain untested.

## Validation infrastructure

Local PostgreSQL 16.15 was extracted into `/tmp` without system installation.
It listens only on localhost port 55432 and contains separate disposable test
databases for control, messaging, and cross-service tests. Local trust auth is
for this synthetic test instance only and must never become production config.
Tests that require sockets need the appropriate sandbox network permission.

## Tests completed before runtime integration

- Full control suite: 258 passed, 3 optional PostgreSQL tests skipped in that
  invocation. Dedicated PostgreSQL races passed separately: 30 concurrent
  signups admit exactly 20; 30 first turns create one activation; 100 attempts
  admit exactly 75 turns. Timezone-aware timestamp fix included.
- Real Hermes runtime: ten tests passed against the exact 9.24 source, including
  durable scoped HTTP ingress, actual adapter/broker processing, restart
  uncertainty, and approval commands during an active task. All thirteen
  pre-existing tenant test suites passed. The image built and its real-source
  gate passed; initial publication failed because the registry namespace used
  the former repository owner. New publication targets
  `ghcr.io/gitupser/squire/hermes-tenant-pilot`.
- Model proxy: nineteen tests passed. A full local LiteLLM 1.94.2/Prisma/Postgres
  smoke with a synthetic upstream recorded three native Anthropic requests,
  charged cache usage correctly to one shared sponsor team, accumulated $11.52
  in synthetic spend, and rejected the next request with 429 before contacting
  the provider. Prompt/response bodies were not persisted. This involved zero
  paid provider calls. The $10 limit is an accrued spending cutoff with possible
  in-flight overshoot, not an exact prepaid ceiling; requests are concurrency
  bounded and output is capped at 8192 tokens.
- Messaging: 24 PostgreSQL/network cases passed, including concurrent leases/checkpoints,
  scope isolation, readiness, group approval, receipts, deletion, and private
  approval/result release. Further review regressions are being added.
- Transport: 14 Python cases passed using the actual patched executable;
  upstream store and app suites also passed with the provenance patch.
- Real Chromium desktop/mobile acceptance passed with actual control, messaging,
  provisioning lifecycle and Caddy. It covered two signups, first outbound
  verification before inbound, trial activation, replay, scoped recipient
  rejection, owner/admin group approval, private-result release and pause.
  Railway, email, provider, model, and runtime execution were loopback fixtures.
  This does not prove actual Hermes execution or WhatsApp delivery.

## Outstanding release gates

Runtime adapter and compatible image; cross-service behavior and concurrency
tests; independent code review; immutable build/deployment IDs; configured
production databases, model proxy and email; restore rehearsal; phone pairing;
founder migration and rollback drill; fresh customer website signup and actual
WhatsApp receipt; approved group isolation; founder soak and staged admission.

References: [Resend verified domains](https://resend.com/docs/dashboard/domains/introduction),
[sending-only API keys](https://resend.com/docs/api-reference/api-keys/create-api-key).

## Verification update: deployed pilot services and migration rehearsal

Commit `51bbc0c` records the signup, lifecycle, scoped messaging, transport,
backup tooling, and browser acceptance harness. Runtime follow-up `9e55ac3`
fixes the public Camoufox archive font path; its image build is pending.

Latest verified production deployments (all SUCCESS):

- Control API `ada294f5-0ecc-48e0-97b2-e7f083783b18`; worker
  `0e00274d-41ec-4d6f-a827-e06e186952b9`. Database revision 2 is verified.
  Admission, provisioning worker, and lifecycle worker remain disabled.
- Messaging API `1d70cf11-4a77-45dc-940b-eafb6c4c1d23`; worker
  `60d4722b-1e34-48af-9e14-eb2004e8540a`. Outbound remains disabled.
- Transport `5357d849-f25e-4fec-a67e-9cb0e1e029a3`; attachment and dedicated
  encrypted-backup endpoints installed. Pairing mode and outbound-off verified.
- Web `72c1cd25-bb45-4a2c-b77e-56768aa220ca`; public HTTPS returns 200 and
  the operator route returns 404.

Current checks: control 275 passed including PostgreSQL races; messaging 32
passed against PostgreSQL; transport 19 passed including pinned wacli and both
loopback address families. The browser acceptance harness passed two signups,
first outbound phone challenges, independent runtime/backup capabilities,
approved group participation, private-result approval/release, pause, and mobile
layout. These use simulated provider services, not live WhatsApp or paid models.
An earlier full-suite quota test allowed 100 instead of 75; the isolated test
and complete rerun passed. Diagnostic assertions are retained; root cause has
not been established and this must remain visible during canary review.

The media relay now releases its database connection before transport I/O and
limits concurrent downloads to one per runtime and four per process, with a
fresh authorization check before releasing buffered bytes.

The founder archive restored locally with 25,833 verified file checksums and
seven SQLite integrity checks. All three service PostgreSQL archives restored
into a disposable service. The original Hindsight database also restored: one
bank, 14,324 memory units, 1,132 documents, and 5,228 entities. However, a real
0.9 upgrade rehearsal removed 2,712 memory records. Its preservation gate
correctly failed; no founder cutover is authorized by this evidence. Determine
a compatible memory runtime or a separately validated preservation strategy
before promotion. Matching embedding dimensions alone is insufficient: source
embeddings use local BAAI/bge-small-en-v1.5 (384 dimensions), with RRF reranking.

The source agent remains running. Private package pull credentials, a valid
service-funded model key, primary-phone pairing, actual runtime canary, and
fresh-tester WhatsApp acceptance remain required. No live signup-to-WhatsApp
completion or founder migration is claimed.
