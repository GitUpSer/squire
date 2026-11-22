
## WhatsApp pilot deployment

The pilot is off by default. Its additive migration creates only explicitly
allowlisted tables, uses a Postgres advisory migration lock, and verifies existing
pilot column names. Run `python -m control_api.pilot_migrations` before admission;
startup repeats this safely. It does not silently ALTER old schemas. Pilot dates
are TIMESTAMPTZ. There are no phone numbers, message text, or model keys in these
tables; capability and login values are HMAC digests.

Required configuration before enabling customer signup:

- `PILOT_ENABLED=true`, `PILOT_ADMISSION_OPEN=true`,
  `PILOT_WORKER_ENABLED=true`, `PROVISION_WORKER_ENABLED=true`.
- `PILOT_SECRET`: independently generated, at least 32 random characters. Stable
  across deployments (rotation invalidates sessions and runtime capabilities).
- `INTERNAL_API_TOKEN`: separate operator credential; the development default is
  rejected when pilot routes are enabled. Never give this to pilot runtimes.
- `PILOT_WEB_ORIGIN=https://app.thesquire.co`, `PILOT_COOKIE_SECURE=true`.
- `MESSAGING_BASE_URL`: private messaging origin; `MESSAGING_SERVICE_TOKEN` must
  match messaging's `MESSAGING_CONTROL_TOKEN`.
- `CONTROL_MESSAGING_SERVICE_TOKEN`: dedicated credential for messaging callbacks.
- `RESEND_API_KEY`, `RESEND_FROM="Squire <hello@your-verified-domain>"`.
  `RESEND_API_URL` defaults to `https://api.resend.com`. Create/verify the sender
  domain in Resend, publish its supplied DNS records, create a sending key and
  test delivery to the founder before opening admission. Missing sender/key or
  failed provider delivery returns 503; there is no exposed-code fallback.
- Existing Railway placement/image credentials and `LITELLM_BASE_URL`,
  `LITELLM_MASTER_KEY`, `TRIAL_BASE_URL` as documented above. LiteLLM must be backed
  by its own persistent Postgres. Team budgets never reset; runtime keys share
  the sponsor's $10 team. Reissuance revokes the deterministic alias first.
- Defaults: `PILOT_SEAT_LIMIT=20`, `PILOT_TRIAL_HOURS=72`,
  `PILOT_DAILY_TURN_LIMIT=75`, `PILOT_GROUP_LIMIT=1`.

Create an invitation using operator-authenticated
`POST /v1/operator/invitations {"email":"recipient@example.com"}`. Distribute the
returned fragment URL privately. Only that verified email may reserve its seat.
Reservations expire after 30 minutes; accepted seats remain consumed. The founder
import consumes one of the same twenty seats. Trial activation occurs on the first
inbound turn after readiness, not on signup. That turn remains queued while the
proxy key is created and the runtime redeployed. The first heartbeat from the new
deployment must confirm the model is ready before admission succeeds.

### Runtime contract

Pilot provisioning skips the Telegram pool, Telegram webhook, and old shared
control token. Each isolated private/group runtime gets:

```
SQUIRE_RUNTIME_ID=<runtime id>
SQUIRE_RUNTIME_KIND=dm|group
SQUIRE_OWNER_TENANT_ID=<sponsor tenant>
SQUIRE_CONVERSATION_ID=<opaque messaging conversation id>
SQUIRE_MESSAGING_URL=<private messaging origin>
SQUIRE_EGRESS_TOKEN=<runtime-scoped capability>
SQUIRE_INGRESS_TOKEN=<runtime-scoped capability>
SQUIRE_CONTROL_TOKEN=<runtime-scoped capability>
SQUIRE_DEPLOYMENT_GENERATION=<integer, initially zero>
CONTROL_API_URL=<private control origin>
SQUIRE_DISABLE_PRIVATE_TOOLS=true  # groups only
```

Messaging sends canonical events to the registered private `POST /events` endpoint
using the runtime ingress capability. Only that runtime's conversation can be sent
back via its egress capability. The runtime must send
`POST /v1/runtime/{runtime_id}/heartbeat`, bearer `SQUIRE_CONTROL_TOKEN`, with
`{ready:bool,model_ready:bool,deployment_generation:int}`. A superseded deployment
receives 409. Boot without a model credential is required: `ready=true` and
`model_ready=false` allows the first deterministic introduction, while model
activation remains gated. Every new deployment should beat immediately.

Messaging service callbacks, bearer `CONTROL_MESSAGING_SERVICE_TOKEN`:

- `GET /v1/service/tenants/{id}/entitlement?runtime_id=<optional group runtime>`:
  returns allowed/state/expires_at/runtime_ready; fail closed on dependency errors.
- `POST /v1/service/tenants/{id}/admit {operation_id,runtime_id}`: idempotent shared
  DM/group turn admission. Retry activating/runtime_unavailable without losing input.
- `POST /v1/service/groups/activate {tenant_id,conversation_id,claim_id}`: provision
  a separate service, volume, DEK and capability set after messaging verifies admin
  authorization. The sponsor owns usage and management, never group memory access.
- `POST /v1/service/tenants/{id}/state {state:paused|expired|deleted}`: STOP and other
  lifecycle events deny admission immediately and queue durable cleanup.

Pause/expiry/deletion cleanup revokes model keys and stops services; deletion also
removes volumes/services through the existing teardown flow. External cleanup
failures remain pending. A connection-held Postgres advisory lock prevents stale
lease workers overlapping side effects; lease generations fence completion writes.
Group removal revokes the messaging grant immediately and denies runtime admission.

### Founder import

Operator `POST /v1/operator/founder` accepts `email`, `tenant_id`,
`conversation_id`, `service_id`, `volume_id`, `service_name`, `internal_url`,
`display_name`, `timezone`, `encryption_confirmed:true`, and
`consent_version:"pilot-2026-11-15"`. It adopts a prepared service/volume and queues
scoped variables/registration. It never copies files or reads model credentials.
The migration operator must first rehearse backup/restore and remove old shared
service variables from the imported service. No private credentials are copied to
groups. Founder entitlement is `imported_active` with no trial expiry; its existing
DM credentials are preserved. Separate group keys use the capped, non-resetting
team budget with no time expiry. Founder migration is not complete until real
WhatsApp round trips and group isolation checks pass.

### Validation

`python -m pytest tests` runs the unit/HTTP regression suite.
`PILOT_TEST_POSTGRES_URL=postgresql+psycopg://... python -m pytest tests/test_pilot_postgres.py`
runs 30-way signup (20 seats), concurrent trial activation, and 100-way quota
races against isolated temporary schemas in a disposable Postgres database.

Private GHCR images optionally use `TENANT_REGISTRY_USERNAME` and
`TENANT_REGISTRY_PASSWORD` (read-only registry token). These are sent only through
Railway's `registryCredentials` create/update fields, never tenant environment
variables. Railway plan support is required; creation fails rather than silently
falling back to another image. Keep the token on the control API/worker only.

Every runtime provisions a deterministic `backup-<runtime_id>` Railway bucket and
receives references to only that bucket's capability. Imported founder services
may use `PILOT_FOUNDER_BACKUP_BUCKET` to adopt an already prepared bucket. No shared
backup credentials are installed in customer or group runtimes. Bucket retention
is separate from live service/volume deletion and must follow the stated backup
retention policy.

### Idle setup and explicit extensions (schema revision 2)

A verified customer who has not sent a first message is paused after 24 hours.
`POST /v1/trial/resume` (authenticated customer with CSRF) resumes setup before
seven days from verification. Resuming does not start or shorten the trial clock.
After seven days without activation, durable cleanup deletes live resources and
then releases the unused pilot seat. Founder imports and activated trials are
excluded. Provisioning, activation and lifecycle cleanup share one sponsor lock;
a stale job cannot deploy after teardown.

Operator `POST /v1/operator/tenants/{tenant_id}/extension` accepts:

```
{"operation_id":"unique-grant-id","operator_label":"pilot-operator",
 "reason_code":"pilot_evaluation","expires_at":"2026-11-20T00:00:00Z",
 "total_budget_cents":1500}
```

The explicit absolute cap is $15 total in this example, including all prior
spend. The worker updates the existing shared team budget without resetting its
spend or trial-day counters, rotates trial keys, and restarts eligible scopes.
It does not revive a separately revoked group or override a user pause. The
operator endpoint accepts only active/expired customer trials; founder private
credentials retain their original provider settings. Allowed reason codes are
`pilot_evaluation`, `service_recovery`, and `customer_support`. Deadlines must be
within 30 days and caps between $10 and $1,000. Decisions and completion status
are durable, inspectable at `GET /v1/operator/tenants/{tenant_id}/extensions`.
Retries require the identical operation ID and decision. Customer extension
requests alone never change entitlement or funding.

Run `python -m control_api.pilot_migrations` before deploying revision 2. It adds
only the explicitly allowlisted `pilotextension` audit table and revision marker;
it neither rewrites existing tables nor performs a silent schema alteration.

Founder import additionally requires `PILOT_FOUNDER_IMAGE` as an immutable
`repository@sha256:<64 hex>` reference. Its compatibility image preserves the
verified founder Hindsight version; new customers and new group runtimes always
use `TENANT_IMAGE`. There is no fallback from a missing founder image to a newer
schema. All pilot runtimes pin BGE-small-en-v1.5 local embeddings (384 dimensions)
and RRF re-ranking across LLM-provider changes. Equal vector dimensions alone do
not make a different embedding model compatible with existing memory.
