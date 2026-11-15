# WhatsApp pilot transport

Private single-account broker pinned to wacli `v0.26.0` commit
`535a8b969cc83c3aad57f66be77e8fd4a7fcaaa3`, plus
`patches/0001-durable-message-origin.patch`. Production identifies itself as
`0.26.0-squire.1`. Original release archive checksums and patch SHA-256 are
recorded in `wacli-release.json`. `scripts/install_wacli.py` installs the
original verified binary for offline contract testing, not production.

The patch atomically records live/history origin with each message mutation
in `squire_change_origins`. Only the live SDK handler without `SourceWebMsg`
can assert live provenance. History, unknown origins, and unavailable-message
recovery carrying `SourceWebMsg` are conservatively quarantined. Timestamps
alone never grant live origin. Older journal entries also cannot claim that
a newer payload mutation was their original live content.

## Deploy and pair

Build from repository root:

```sh
docker build -f apps/whatsapp-transport/Dockerfile -t squire-whatsapp-transport .
```

Use one replica, no sleep, no rolling overlap, private networking, and an
encrypted persistent `/data` volume writable by UID/GID 10001. The mount
check and exclusive `transport.lock` prevent accidental ephemeral or second
writers. SQLite commits use full synchronous durability. Copy `.env.example`
and create three distinct random secrets: broker (messaging → transport),
operator (operations only), and `MESSAGING_TRANSPORT_TOKEN` (transport →
messaging). Never mount this volume or supply these credentials to tenants.

Use `/healthz` for deployment liveness, including before pairing. `/readyz`
returns 503 until connected and capture activated. Operator-authenticated
`GET /v1/status` reports connectivity, terminal logout, capture errors,
last committed work, generation, outbound state and capabilities. Server
binds `::` for Railway private IPv6; `TRANSPORT_HOST` overrides this locally.

1. Deploy with `TRANSPORT_PAIRING_MODE=true` before pairing. This keeps the
   liveness endpoint/SSH container available, holds no account lock, starts
   no sync/capture/retention workers, and rejects all broker mutations. In a private terminal on the
   persistent volume, run `squire-whatsapp-pair`. It rejects redirected
   output and a running broker. Scan the QR using the approved account's
   phone. Optional `squire-whatsapp-pair --phone` prompts privately for a phone
   pairing code instead of QR. Never send pairing output to shared logs, tickets or chat. Pairing
   requests only one day / ten messages per chat of initial history.
2. Redeploy with `TRANSPORT_PAIRING_MODE=false`, keeping outbound false. It supervises one `wacli sync --follow`. Terminal logout
   disables sends and prevents reconnection until explicit pairing clears
   the marker. Normal exits use bounded backoff, not idle restarts.
3. Configure messaging for the same provider account. Operator-authenticated
   `POST /v1/operator/activate` baselines the store and starts durable
   capture. Repeat activation after a lost acknowledgement safely. Earlier
   history cannot run customer actions.
4. After application/live gates pass, coordinate generation with messaging
   and POST `/v1/operator/control` with
   `{"generation":1,"outbound_enabled":true}`. Outbound defaults false.
   Higher durable generations fence old senders; lower generations fail.
   Disable outbound through this endpoint before rollback/cutover.

Pairing does not migrate or disable the existing personal agent. Its
single-writer cutover and state import belong to the founder migration runbook.

## API and durability

Messaging uses the broker bearer token:

- `POST /v1/send`: `operation_id`, `provider_account_id`, `generation`,
  explicit `recipient`, `kind:"text"`, `text`, optional `reply_to`.
- `GET /v1/groups/{jid}`: live participant snapshot with original `jid`,
  verified `phone_jid` when supplied by the SDK, `admin`, and fetch timestamp.

Only explicit phone, PN/LID and group JIDs are accepted. Display names are
never destinations. Messaging owns tenant/recipient/consent authorization.
Send results are `accepted`, `definitely_unsent`, or `uncertain`; accepted is
not delivery. A durable intent precedes dispatch. Restart marks in-flight
dispatch uncertain. Identical accepted/uncertain operations never resend;
changed payloads under the same operation ID fail. Only proven pre-dispatch
failures are retryable, including a compare-and-swap of a known-unsent ledger
entry under the dispatch mutex. Raw CLI output/argv and HTTP access logs are
never emitted.

Capture polls `messages changes` in pages of 100, fetches current payloads,
and POSTs `/v1/ingest/batches`. Messaging atomically commits events and cursor.
Every poll first reads `/v1/ingest/checkpoints/{store_id}`, recovering from
lost acknowledgements. Missing/expired/mismatched cursors fail readiness and
sends rather than silently rebasing. Actual expired journals require an
audited export/reconciliation; no discard-and-resume endpoint exists.

Receipts are absent upstream's changes journal. The service reads
`wacli.db.message_receipts` read-only, maintains a deduplicated durable local
receipt journal, and commits it using `{store_id}:receipts`. Receipt identity
is per message and recipient. Unknown LIDs remain unbound until verified;
aliases are never inferred from names or text.

Live group-info and operator `POST /v1/groups/{jid}/leave` are not delegated
upstream. They pause sends under the command mutex, fully stop sync, run a
bounded command, then resume sync and replay. No simultaneous second socket
is opened. Fresh metadata is necessary for group authorization. Native
inbound mentions are unavailable; use `!squire` or quoted replies.

## Attachments

Authenticated `POST /v1/media/fetch` accepts only
`{provider_account_id,chat_jid,provider_message_id}`. It verifies an existing,
nondeleted stored message with known length at most 10 MiB, then uses
`wacli --read-only media download` without opening another WhatsApp socket.
Image, audio, video and document bytes are returned with provider MIME type,
safe filename and `X-Media-Type`. No caller URL/path is accepted. The upstream
CDN fetch verifies encrypted/plaintext checksums and declared length; its own
100 MiB network ceiling applies before our 10 MiB verified response ceiling.
Downloads serialize to bound memory. Temporary plaintext is removed on success,
failure and client disconnect; there is no persistent media cache.

`POST /v1/send` also accepts `kind:"media"` and
`media:{data_base64,mime_type,filename,media_type,ptt:false}`; text is an optional
caption. Types are image/audio/video/document, with decoded bytes capped at
10 MiB and a 120-character filename. Voice notes require `audio/ogg`, `ptt:true`
and upstream Ogg Opus validation. Private temporary files are passed to
`send file --max-size-mb 10 --as <type>` through the existing sync owner's
delegated queue. The same generation fence, payload fingerprint, durable
intent and no-retry uncertain outcome rules apply to media. No media content
is persisted in the transport ledger.

## Retention

Operator `POST /v1/operator/retention` accepts
`{"days":7,"limit":100,"dry_run":true}`. Setting `dry_run:false` performs
local payload purge; it never transmits a WhatsApp deletion. Only durably
captured records are eligible. It stops sync and drains capture, writes
local tombstones through the verified pinned SQLite schema, commits those
events to messaging, calls upstream `messages purge --confirm`, then commits
the resulting deletions. Acknowledging the tombstones before purge prevents
purge's journal cleanup from overtaking the committed checkpoint. A failed
acknowledgement stops before purge. Repeating is safe.

The supervised retention loop runs hourly after capture activation, in bounded
batches of 100. `TRANSPORT_RETENTION_ENABLED=false` disables it during a
rehearsal; interval and days are configurable. Failures are exposed in operator
status and never bypass capture acknowledgements. Baseline history skipped at activation stays
ineligible and needs migration-policy cleanup. The upstream build does not
expose disappearing-message TTL metadata; shorter account-specific retention
is a capability gate. Backups and minimal
receipt/dedup metadata need the deployment operator's 30-day lifecycle;
payload purge does not erase backup archives. Monitor volume usage (wacli
defaults to a 2 GB cap), capture errors, disconnected sessions and uncertain
sends. Keep store/session/ledger in one backup generation.

## Tests and remaining live gates

```sh
uv venv /tmp/squire-transport-venv
uv pip install --python /tmp/squire-transport-venv/bin/python -e 'apps/whatsapp-transport[test]'
WACLI_TEST_BINARY=/path/to/pinned/wacli /tmp/squire-transport-venv/bin/python -m pytest apps/whatsapp-transport/tests -q
```

The real CLI fixture uses an unpaired temporary store to test capture,
receipt deduplication and purge cursor continuity. Go tests prove provenance
rollback and history isolation. No test pairs or sends. Required live gates
remain pairing, first-contact send and receipt, group roundtrip, reconnect
after group metadata lock handoff, founder migration, and second signup.
