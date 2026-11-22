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
| control-api | `e6f51482-3db5-423f-8d76-3a1db48c0641` | SUCCESS; admission closed; reviewed control v5 deployed |
| control-worker | `c5e4b1e8-4fa6-47e8-a6f7-ad1bdfb4ce3e` | SUCCESS; processing disabled |
| messaging-api | `e2471ffb-9747-4e48-8359-9f656a6e3d3c` | SUCCESS; explicit pre-deploy migration |
| messaging-worker | `bdf3a124-3bc3-4c80-befd-fd1f171f9dff` | SUCCESS; outbound disabled |
| trial-proxy | `40a0c990-bb27-4eb4-a46d-e35b7bf384ad` | SUCCESS; replacement Anthropic credential required |
| wacli-transport | `880eca27-7b70-4b62-adf8-e84e465e2bf3` | SUCCESS; pairing mode; outbound disabled |
| app-web | `db10d96a-e7f4-4255-ac94-4c03b0590644` | SUCCESS; HTTPS 200; operator API paths 404 |

Initial deployment IDs (superseded by the verification update below): control API `a4ce6351-a8e5-4d04-92eb-9e4be6eed836`,
control worker `e70dec68-6bf1-4de3-84b0-a26f5deb6fbd`, messaging API
`201e2828-7459-4b3f-a31f-847e64940d3c`, messaging worker
`46e37af0-5530-4df8-b385-bdc2d573109b`, proxy
`e20cfe49-5078-4c99-948e-44ca1f91fd09`, frontend
`0077bde3-28af-40af-a6c0-740148c3e4e4`. These are initial disabled deployments;
subsequent review fixes are recorded below.

Transport volume `588bc84f-7697-4466-84ad-0e3a671aa28c`: 5120 MB at `/data`,
us-west2, one replica, no sleeping, no deployment overlap. Initial healthy
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

Private registry pull credentials and a valid service-funded model key; actual
runtime canary with restored founder data; phone pairing; final fenced founder
snapshot, migration and rollback drill; fresh customer website signup and actual
WhatsApp receipt; live approved-group isolation; founder soak and staged admission.
Implementation, independent review, synthetic acceptance, image build gates,
production databases/email, and archive restore rehearsals have passed the
checks recorded below. These do not replace the remaining live gates.

References: [Resend verified domains](https://resend.com/docs/dashboard/domains/introduction),
[sending-only API keys](https://resend.com/docs/api-reference/api-keys/create-api-key).

## Verification update: deployed pilot services and migration rehearsal

Commit `51bbc0c` records the signup, lifecycle, scoped messaging, transport,
backup tooling, and browser acceptance harness. Runtime build rc7 and the latest
CI results are recorded below.

Latest verified production deployments (all SUCCESS):

- Control API `f1dbbff6-f8ee-45ac-a395-55e0e53aa51e`; worker
  `ab7a3fe7-7a52-4a69-ac8f-4136ea45b7d5`. Database revision 2 is verified.
  Admission, provisioning worker, and lifecycle worker remain disabled.
- Messaging API `1d70cf11-4a77-45dc-940b-eafb6c4c1d23`; worker
  `60d4722b-1e34-48af-9e14-eb2004e8540a`. Outbound remains disabled.
- Transport `5357d849-f25e-4fec-a67e-9cb0e1e029a3`; attachment and dedicated
  encrypted-backup endpoints installed. Pairing mode and outbound-off verified.
- Web `72c1cd25-bb45-4a2c-b77e-56768aa220ca`; public HTTPS returns 200 and
  the operator route returns 404.

Current checks: control 283 passed including PostgreSQL races; messaging 32
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
correctly failed. The matching-version founder variant described below preserves
these records in the database rehearsal; restored-image canary and recall checks
remain required before cutover. Matching embedding dimensions alone is insufficient: source
embeddings use local BAAI/bge-small-en-v1.5 (384 dimensions), with RRF reranking.

The source agent remains running. Private package pull credentials, a valid
service-funded model key, primary-phone pairing, actual runtime canary, and
fresh-tester WhatsApp acceptance remain required. No live signup-to-WhatsApp
completion or founder migration is claimed.

### Matching-version memory rehearsal and cleanup

The 0.9 upgrade removed exactly 2,712 derived observations; world/experience
facts and surviving memory contents did not change. A separate 0.5.2 rehearsal
kept the original schema head `h3i4j5k6l7m8`, all 14,324 memory units, and the
checked bank/document/entity/content hashes unchanged. The founder will use a
separate image pinned to that memory version and pg0 0.12.3. New-customer images
remain on 0.9. Actual image startup and recall parity are still required.

The copied queue contained 6,766 completed and 224 failed operations, with no
pending/processing work. Keep the memory worker disabled during pre-cutover
checks and recheck the final fenced snapshot rather than assuming this remains
true. The scratch PostgreSQL service and volume were deleted after preserving
the rehearsal reports locally. Its PostgreSQL 18.7/vector 0.8.7 differ from the
source embedded versions; this is not evidence of exact image compatibility.

Founder core (4,375,882,984 encrypted bytes), integrations (628,090,776 bytes),
and reviewed source-mode metadata (8,050,792 bytes) have all been uploaded to
the encrypted backup bucket, downloaded, and verified against their SHA256.
Private decryption keys remain local. Backup worker deployment
`d0c1ab79-fdb6-4fc7-991c-8f8281be35e6` is SUCCESS with retention covering the
founder archive namespaces.

Five additional quota race runs passed. Pilot CI run `43760452229` passed the
PostgreSQL/browser/model-accounting suite and all six service image builds.
Runtime image candidates rc3 and rc4 failed the actual browser build gate;
font-resource mapping and an upstream missing `await` were identified. A new
image candidate must pass before any tenant image is promoted.


## Verified runtime release and current CI

Tag `tenant-pilot-v0.4.0-rc7`, commit
`bf8ed3d2d4cda03d48b59b1cbc4214adb07c2f14`, passed build run
[43762913671](https://github.com/GitUpSer/squire/actions/runs/43762913671).
Both variants launched the actual embedded memory service with Hermes client
0.10.3 and passed HTTP health, bank creation, empty recall, and deletion. Both
also launched Camofox 1.11.2 / Firefox 135 beta.24 and passed the actual page and
snapshot check. This is synthetic image verification, not restored-memory recall
parity or a deployed tenant canary.

Immutable image references in `ghcr.io/gitupser/squire/hermes-tenant-pilot`:

| Use | Digest |
| --- | --- |
| New customers and groups | `sha256:071a77c0a2fd5b2720b6d082a8b47bcc258f4890974f461fc2737bba810572ea` |
| Imported founder DM, matching Hindsight 0.5.2 | `sha256:8570bd6a9803c17c7b631815992782184f16889cd66dedb65e36da58ab3e1c25` |

These are configured as `TENANT_IMAGE` and `PILOT_FOUNDER_IMAGE` on both control
services, together with `PILOT_FOUNDER_BACKUP_BUCKET=pilot-founder-dm-backups`.
The values were initially saved without a deployment and are included in the
subsequent control v5 deployment. Provisioning and admission remain disabled;
private registry credentials are still required before runtime creation.
Registry visibility remains private; organization policy was not weakened.

At commit `c17dfa41cde4c3e327378bc0b3f1f1a2b36d9aac`, all three workflows passed:
[test](https://github.com/GitUpSer/squire/actions/runs/43763705672),
[tenant-image checks](https://github.com/GitUpSer/squire/actions/runs/43763705585),
and [pilot acceptance](https://github.com/GitUpSer/squire/actions/runs/43763705569).
The latter includes all six service image builds and PostgreSQL/browser/model
accounting acceptance. The tenant checks now include 34 migration/backup tests
using actual pinned Hermes source and verified age tools. Local runtime and
migration checks also passed, including restored-home adoption and source-mode
preservation. No actual WhatsApp delivery has been tested yet.


### Admission state regression and deployment

A separate deterministic regression showed that admission could use stale
quota/pause fields when its SQLAlchemy session had loaded the rows before
another transaction updated them. Acquiring the row lock did not refresh those
cached attributes. Admission now explicitly refreshes entitlement and runtime
reads after the lock. Three SQLite and three PostgreSQL regressions verify
spent quota, paused entitlement, and paused runtime behavior. This does not
establish the cause of the earlier HTTP concurrency failure, whose requests
used fresh sessions and whose original row diagnostics were not retained.

Control validation passed 283 tests: 276 in the full-suite invocation, plus all
seven PostgreSQL cases after correcting the test URL to explicitly select the
installed psycopg driver. The initial generic PostgreSQL URL caused seven setup
errors for absent psycopg2; these were test invocation errors, not application
failures. Deployment context manifest SHA256:
`f1530a336d8266a9e9559be496a632ed1c007b13c56226d44924769c40712b96`.
Both control v5 deployments listed above reached SUCCESS.


### Verified historical session provenance

The independent metadata audit found exact Hindsight routing evidence for six
of 174 untyped historical WhatsApp sessions. All six match the already verified
founder DM; actor IDs, display names, and message contents were not used to infer
routing. The offline helper pins both source archives and the reviewed candidate
manifest, re-extracts metadata, rejects conflicts, and creates a separate 0600
database. Thirteen focused tests passed, including standalone WAL-mode output.

The real archive rehearsal exposed and then verified a fix for SQLite journal
checkpointing before publication. Independent immutable reads of the corrected
output show 38 DM, six group, and 168 untyped sessions (212 total), compared with
32 DM, six group, and 174 untyped sessions in the unchanged original. The output
hash is `396d94ad10f0917ce2d82fdbf20c45895ef421e6e8711b73d0ce2774de7a3262`.
It remains an offline intermediate: ordinary private-history remapping must
remove other conversations and old pending operations before promotion. The 168
unresolved sessions remain archived, and full active-history parity is not claimed.


### Standalone migration verification after WAL fixes

The same SQLite lifetime issue affected existing offline history remapping and
cron output. A shared offline writer now commits, checkpoints the WAL with an
explicit busy check, and closes before hashing or publishing the main database.
Regression tests failed before the fix and now cover standalone history removal,
committed-only backup, cron data, actual Hermes group routing, and busy rejection.
The combined migration/backup suite passed all 51 tests.

A separate copy of the actual hydrated archive was then remapped to a synthetic
rehearsal conversation. Immutable SQLite verification found exactly 38 Squire DM
sessions, one active route, zero old pending operations, and no foreign-key
errors. Groups and unresolved sessions were absent from this standalone output.
The source archives remain unchanged. Output SHA256:
`9ebae9649509585eedf496e898283195e439ea75cf05fa9b277243f70902edb7`.
This proves offline transformation behavior, not production identity mapping or
permission to start a migrated runtime. Code is recorded in commit `6226714`.


### Model-accounting CI harness correction

At `215d026`, control and runtime CI passed; pilot acceptance reached all six
successful service builds but its accounting proxy could not bind fixed port
58748 on the runner. The process had exited, but the harness retried readiness
until timeout and cleanup masked the original error. The test harness now uses
OS-assigned ports and inherits a continuously reserved dual-stack listener through
CLI migrations, detects terminal child exit immediately, and safely reaps exited
or unresponsive children. Production listener behavior is unchanged and remains
covered by its IPv4/IPv6 test.

All 22 proxy tests passed. An actual local proxy/Prisma/accounting run also passed
while ports 58747 and 58748 were deliberately occupied: three synthetic requests,
shared team spend 11.52, subsequent 429 budget denial, output capped at 8192,
no persisted prompt bodies, and zero paid provider calls. CI is rerun for this
harness-only correction; there is no production redeployment for it.


## Live credential and cold-canary follow-up

The user-provided GitHub credential was retrieved after reconnecting the browser,
verified as `read:packages` only, and successfully fetched both immutable runtime
manifests. It is installed in the two control services; the registry credential
is not injected into tenant environments. Control API deployment
`6e011ce4-8298-48c2-a7a3-237bac99dce9` and worker deployment
`a4e0fa23-674e-47ff-bd6f-99534e553f86` reached SUCCESS with admission/workers
still disabled. The user-identified Anthropic key in ignored `env.ignore` is
identical to the already deployed key; both read-only validation endpoints
returned 401 `invalid x-api-key`. A replacement was requested, without exposing
it or spending tokens. The second tester invitation is saved privately and has
not been delivered or consumed a seat.

A private standard canary was created as service
`2dc2ff7e-aa9e-4b1f-b028-673a90702b66`, volume
`60faaf0d-acc1-4212-adc4-461939c5e3fc` at `/opt/data`. It has no production
broker binding, no model key, no account socket, and unreachable loopback
broker/control URLs. Deployment `21d854a8-e0aa-450a-b92a-59cba2de13c4` pulled rc7
successfully and verified memory/browser/web health, exact package versions,
sealed credentials, mounted storage, and empty queues. Its gateway failed the
actual startup parser because `home_channel.platform` was absent. The gateway
was stopped while both new-runtime and founder configuration writers were fixed;
168 actual-source overlay/transform tests now pass, including the reproduced
startup-parser regression for DM and group runtimes.

A separate actual-source investigation found that persisted running intent could
let upstream s6 start another gateway before Squire's validated entrypoint.
Patch 008, first built as rc8, skips that reconciliation only for scoped Squire
runtimes; all six actual-source behavior gates pass and legacy behavior remains.
The combined rc9 candidate must pass its build and actual cold-start/restart
checks before replacing the configured rc7 images. Neither rc7 nor rc8 is
accepted as the final provisioning image on this evidence.

An empty founder-canary service `59d36ae4-d735-4eca-ae2b-629d48860e57` with
20-GiB volume `d9bd5001-b920-4969-99e7-8d28c6e06b31` is reserved for the scoped
restore rehearsal. It has no image yet and its start command is `sleep infinity`.
No source credentials, source agent state, or live routing has been changed.

### rc9 deployed canary evidence

Release `tenant-pilot-v0.4.0-rc9`, commit
`459f5c6b1da0ff9759d58a2aa3368c6f69b93c37`, passed
[both image builds](https://github.com/GitUpSer/squire/actions/runs/43815442682),
[control tests](https://github.com/GitUpSer/squire/actions/runs/43815449109),
[runtime checks](https://github.com/GitUpSer/squire/actions/runs/43815449223), and
[pilot acceptance](https://github.com/GitUpSer/squire/actions/runs/43815449077).
It includes the actual-source gateway ownership guard and the home-channel
platform parser correction. Both private registry manifests were fetched and
their immutable digests verified:

| Variant | Digest |
| --- | --- |
| Standard | `sha256:9bb726217f2fd65bf68f64093191e4ad0e91ebe9edf689de2f9c93ce63a90150` |
| Founder | `sha256:5139a5ad640ced720e35b92b428731c953f4bfa91a6eabb169b4c6ab5671ae6f` |

Standard deployment `046e608e-b58b-40e3-84e2-9a428ade3f5b` passed cold startup
and restart on its dedicated volume. A synthetic event accepted through the real
ingress remained byte-for-byte unchanged across restart with model readiness
false. Persisted running gateway intent was deliberately present before restart;
exactly one gateway started. Profile and initialization hashes were unchanged.
Cleanup removed only that synthetic event, and a fresh runtime probe confirmed
zero inbox/outbox/tool rows, live adapter, healthy browser/memory/web services,
the correct packages and scoped configuration, and tmpfs-backed secret links.
These checks made no model requests and sent no customer messages.

Founder deployment `92b241d4-7a85-4e34-b1a2-32b9325888a4` pulled the rc9 founder
image. A positive-list rehearsal home and the verified logical memory dump were
restored to its isolated volume, without owner credentials or active schedules.
The real Hermes Hindsight client recalled a known source fact using local BGE
embeddings and RRF. All 15 table content fingerprints, schema and bank identity,
and queue counts remained unchanged: 14,324 memories, 1,132 documents, 5,228
entities, and zero pending/processing operations. This first recall used an
explicit isolated API/cluster. Normal entrypoint adoption and the native pg0
restart lifecycle are still separate acceptance checks; the source agent remains
the live owner. Control provisioning image references remain rc7 until these
checks are completed; admission, workers, and outbound remain disabled.

### Native database restart defect and rc10 follow-up

The founder's default-image startup exposed a reproducible upstream pg0 defect:
its saved PostgreSQL PID had been reused by Node in the new container. With no
postmaster PID file and no listening database, pg0 0.12.3 nevertheless reported
the database running. The newer pg0 0.15.1 checks health in `info()` but still
rejects a recycled live PID in `start()`. The earlier standard restart happened
to pass; it does not exclude this failure. Neither rc9 variant is accepted for
final provisioning on this evidence.

Commit `2293590a9f0cdf882475ee54ad538d40e1da52ce` adds an API-lifetime startup
lock and verifies actual PostgreSQL ownership before startup. It archives only
proven stale `instance.json` metadata after a clean stop, preserves verified
same-cluster PostgreSQL for API-only recovery, and rejects ambiguous state. It
does not signal an unrelated PID or alter database, WAL, or postmaster PID
files. A stale remaining postmaster PID file requires operator inspection.
Twenty-three focused regressions and the credential-wrapper suite passed;
independent review also checked 16 configuration cases against both exact
Hindsight parsers. The image build now tests an actual live-PID collision,
lock exclusion across exec, preserved database identity and bank contents, and
successful recall after restart in both variants. rc10 must pass those builds
and deployed canary checks before promotion.

Two operator configuration issues were also verified and documented in the
runtime canary runbook: redeploy reused the previous startup snapshot, and an
explicit Squire entrypoint override bypassed upstream's privilege drop. The
canary now uses the image's default startup path. Ownership repair touched only
generated canary files and left their bytes and all promoted source-file hashes
unchanged. Before the restart defect was reproduced, the native API recalled
the known source fact with all 15 database fingerprints unchanged. The original
source agent has not been fenced or modified.

### rc12 accepted images and November 22 verification

Tag `tenant-pilot-v0.4.0-rc12`, commit
`e699de6af069c4549525e4020f3942167c23e05a`, passed
[both published image builds](https://github.com/GitUpSer/squire/actions/runs/43823409626),
[control tests](https://github.com/GitUpSer/squire/actions/runs/43823416216),
[runtime checks](https://github.com/GitUpSer/squire/actions/runs/43823416317), and
[pilot acceptance](https://github.com/GitUpSer/squire/actions/runs/43823416119).
Both actual image builds exercised the live-PID-reuse restart gate. The complete
overlay/restored-home/pg0 suite passed 190 tests. rc10 was stopped by a unit test
scanning unrelated protected CI processes; the fixture was isolated without
weakening production checks. rc11 was stopped by the new helper's missing Git
executable bit. Both issues are corrected in rc12.

Accepted repository: `ghcr.io/gitupser/squire/hermes-tenant-pilot`.

| Variant | Verified immutable digest | Canary deployment |
| --- | --- | --- |
| Standard | `sha256:36f7a613c32c883faf0a2ac46de995999cdf10a4dec160d7b83b5edde24a67c2` | `5ea1aa3f-3b43-44ea-a502-8148b77fbf1b` |
| Founder | `sha256:89ee116638a88278b938f157c109d39b8783107210842c3973b224f1413a29d2` | `479db86a-debd-45c5-ae7a-8c4c2eb3ef03` |

The standard canary preserved its exact synthetic pending inbox row, profile and
initialization hashes across a full container restart with persisted running
gateway intent. Only one gateway started. Exact-event cleanup returned all three
queues to zero; fresh November 22 health/readiness checks passed.

The founder canary recovered the preserved stale-PID failure automatically,
then passed another full container restart with a changed PID1 start time and a
second automatically archived metadata file. The real Hermes client recalled
the known original fact among 50 results using local BGE384/RRF. All 15 table
counts and full-row fingerprints, schema, original bank identity/revision, and
operation queues remained unchanged against the verified restore: 14,324
memories and zero pending/processing operations. The fresh probe confirmed one
ready gateway, healthy memory/browser/web services, correct scope and mounted
storage, tmpfs secret links, and zero runtime queues. No owner credentials,
model calls or live deliveries were involved. The original source gateway,
memory daemon and browser were independently checked active on November 22.

The rc12 digests are now selected as `TENANT_IMAGE` and `PILOT_FOUNDER_IMAGE` on
both control services. Admission, provision worker and pilot worker remain
disabled. The migration journal now requires a verified 24-hour founder soak
before the reserved tester flow; fresh website first-contact evidence remains a
mandatory subsequent gate. This removes the previous circular dependency
without treating an existing-contact or synthetic send as fresh delivery.

The service-funded Anthropic key in `env.ignore` still returned 401 on November
22. A replacement and readiness to link the primary WhatsApp phone were
requested together. The saved tester invitation remains undelivered and valid
until November 30; it has not consumed a seat. Model-backed runtime acceptance,
phone linking/capture, final fenced founder import and integration checks, live
groups/media, rollback/soak, and the fresh-recipient signup test remain required.

Control API deployment `dcf1e98a-30d6-4981-b17d-1175b0389ca8` and control worker
deployment `914d1b24-3a5a-447c-81fc-d1924c8d5832` reached SUCCESS with the rc12
image settings. The public pilot app deployment
`d514b88e-8fb7-4682-8c06-a536e5c54de6` adds internal `/trial` and `/trial/` aliases
and explains existing-customer sign-in versus private invitation signup. Live
HTTP checks confirm both aliases and root serve the exact reviewed HTML with
200/no redirects, `/api/v1/me` remains 401 without authentication, and operator
and unknown routes remain 404. No email or WhatsApp message was triggered.

The existing `thesquire.co` marketing site is hosted by service
`3a279bbd-0bc6-4eac-8e65-cee9b90c5139` in project
`ddbcfda7-e6f6-47a9-87cd-903b6efeb8c6`, environment
`0923ea14-9a5e-4d87-990b-be31e5bc76ed`. Its prior live HTML/CSS matched the
repository baseline byte-for-byte. Deployment
`6b6df147-0743-42cd-bbb9-259e76abd646` adds only a discreet pilot sign-in link;
live HTML matches the reviewed source and the waitlist remains intact. The
pre-existing staged patch `a6b229b1-c064-4103-9db5-3cc15b464fde` was not applied
or discarded. Build contexts contained only public site files, Caddy config and
Dockerfile; manifest hashes are
`66c84ca8c04ac6ab619fd23840bba9852da01795697227fb989edf3afd65da49` (pilot) and
`b1febb92bd48e246e47928481c63b71d97dbb42660ccf211325ca6be8fc9732e` (marketing).
