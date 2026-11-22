# Squire messaging plane

Private FastAPI service and independent durable worker for a single provider
account. It requires PostgreSQL. SQLite is allowed only with the explicit
`MESSAGING_ALLOW_SQLITE=true` switch for fast unit tests; it does not validate
production concurrency.

Build both Dockerfiles from the repository root. Run `squire-messaging-migrate`
once as a release command before starting API or worker. The migration acquires
a PostgreSQL advisory transaction lock and installs versioned revision 2. API
startup verifies the revision and does not silently migrate it. Both processes
must use the same keys, database, provider identity and service URLs.

Required environment:

```
MESSAGING_DATABASE_URL=postgresql://...
MESSAGING_ENCRYPTION_KEY=<independent random secret at least 32 chars>
MESSAGING_HMAC_KEY=<independent random secret at least 32 chars>
MESSAGING_CONTROL_TOKEN=<control-to-messaging bearer>
MESSAGING_TRANSPORT_TOKEN=<capture-to-messaging bearer>
MESSAGING_OPERATOR_TOKEN=<operator bearer; never give to tenant runtimes>
MESSAGING_CONTROL_URL=http://control-api.railway.internal:8000
MESSAGING_CONTROL_CALLBACK_TOKEN=<messaging-to-control bearer>
MESSAGING_TRANSPORT_URL=http://whatsapp-transport.railway.internal:8080
MESSAGING_TRANSPORT_BROKER_TOKEN=<messaging-to-broker bearer>
MESSAGING_PROVIDER_ACCOUNT_ID=squire
MESSAGING_OUTBOUND_ENABLED=false
```

Keys and tokens must be independent. Payloads and destinations use AES-GCM with
per-record authenticated context; identities and credentials use domain-separated
HMAC lookup keys. Back up keys separately from the encrypted database. Key
rotation needs a reviewed decrypt/re-encrypt migration; replacing the key env var
alone will make old pending records unreadable.

Outbound is gated twice: configuration defaults disabled and persisted provider
state defaults paused. Operator `POST /v1/operator/provider` controls pause and
monotonic generation. The matching generation must be configured at the broker.
Pause before a generation change. `GET /v1/operator/health` gives only queue
counts, checkpoint ages and gate state. `GET /healthz` and `/readyz` contain no
customer details. Uvicorn access logging is disabled to avoid path identifiers.

Contracts are defined in `schemas.py` and `main.py`; the API has no public docs
endpoint. Every mutating endpoint requires its specific service principal or a
registered runtime token. Runtime egress accepts an opaque conversation ID and
logical response key; it cannot accept a recipient address. Group runtimes have
one group grant and cannot approve/release private results.

The capture checkpoint commits in the same transaction as observations and
encrypted inbox records. API acknowledgement happens after commit. Replay
deduplicates both journal observation and logical provider message identity.
Unknown/history-origin messages never start turns. Only verified protocol PN/LID
aliases are accepted from the trusted capture service. Per-conversation leased
delivery is serial, and admission uses the stable event ID. Runtime durable
acceptance is required; runtime execution and external tool idempotency remain
the runtime's responsibility.

The worker runs independent ingress, outbound and maintenance lanes. PostgreSQL
row locks and monotonic fences prevent stale claims from changing a replacement
worker's state. Global queue limits use transaction advisory locks across API
replicas. Outbound claims serialize the provider account and rotate tenant
selection. The worker renews its lease after readiness and fresh group metadata
checks, then commits dispatch intent before touching the provider. A crash after
that commit becomes `uncertain`; no automatic resend follows. Only a broker
`definitely_unsent` result with `retryable:true` permits automatic retry, subject
to queue TTL. Accepted IDs and receipt states are distinct, and group receipts
remain participant-specific. Unresolved uncertain sends require operator
investigation; there is intentionally no force-resend API.

Phone verification hashes an eight-digit code, caps five attempts, throttles new
challenges to three/hour, and provides a one-use fallback nonce. A forwarded
nonce cannot silently bind a different sender. STOP is deterministic, immediately
suppresses pending sends and durably asks control to revoke entitlement.
Group claim requires the verified sponsor to post `!squire claim <nonce>` plus
a current administrator to approve if the sponsor is not an administrator.
Groups default to `!squire` and quoted-reply activation; pinned wacli does not
expose incoming mention metadata. `!squire mode all` and `!squire stop` require
fresh administrator evidence. Removing the sponsor invalidates the group before
the next dispatch/send. Membership detection delay is bounded by the next
message/send and live metadata roundtrip, not an unsolicited membership webhook.

Completed message payloads expire after 24 hours, pending/dead payloads after
seven days; challenge/claim material expires after its TTL plus 24 hours.
Disappearing messages carry a trusted absolute `expires_at` in Unix seconds;
explicit null means confirmed ordinary. Their earlier deadline blocks dispatch,
media access and parent-linked replies, and the existing retention worker removes
expired live payloads. Outbound deadlines also reach the provider broker.
Private approval results expire at their ten-minute TTL. Deliberate tenant
deletion erases live payloads and credentials immediately, retaining minimal
HMAC/idempotency/consent metadata. Database backup retention is an infrastructure
obligation and must be separately configured and tested.

Run unit/safety tests with `pytest apps/messaging/tests`. Run PostgreSQL races:

```
MESSAGING_TEST_DATABASE_URL=postgresql+psycopg://.../squire_messaging_test \
  pytest apps/messaging/tests -q
```

The test database is destructive and must have `_test` in its URL. The suite
checks transaction/checkpoint races, queue caps across tenants, stale leases,
recipient scope, STOP, group owner/admin evidence, private approval release,
setup receipts, readiness and ambiguous sends. It does not replace phone pairing,
real provider delivery/read evidence, founder data migration, restore testing,
or the browser acceptance harness. Media capabilities and provider media sends
are not implemented in this text messaging API yet; keep founder cutover gated
until those end-to-end acceptance cases are implemented and verified.
