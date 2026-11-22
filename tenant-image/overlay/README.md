# Scoped WhatsApp runtime overlay

This overlay requires Hermes v2026.9.24 (Python package 0.21.5) and registers the
`Squire` platform through its public plugin entry point. The image owns the
managed configuration under `/etc/hermes`; the runtime volume cannot disable the
plugin or relax group tool restrictions. The supported rollback floor is another
image carrying this overlay and the same or a compatible newer Hermes schema.
Never boot the old v8.3 image on a migrated founder volume.

## Durability and isolation

The authenticated `/events` listener validates the complete fixed runtime scope,
then commits a SQLite WAL inbox transaction before returning 202. Duplicate IDs
with identical content are acknowledged; ID reuse with changed content is denied.
Ordinary turns are ordered per runtime. Owner approval/clarification commands can
resolve a waiting private turn without waiting behind that turn.

A process interrupted during a turn leaves an **uncertain** operation. Restart
blocks subsequent ordinary turns and reports the interruption; it does not rerun
model calls or tools. An operator must reconcile external effects, then use the
scoped ingress credential on `POST /events/recovery` with
`{"event_id":"…","decision":"reconciled_skip"}`. There is no exactly-once claim
for arbitrary external tools. Durable reply keys allow broker retries without
creating another WhatsApp send operation.

Patch 007 is a small, pinned scheduler delivery seam. It carries the persisted
Hermes execution ledger's job ID and scheduled occurrence through both live and
standalone delivery lanes. Replaying the same occurrence/content queues the same
outbound key, including fallback after a timeout. Explicit manual executions have
new attempt IDs and intentionally represent new sends. This does not authorize
rerunning a job's tools after an unknown execution outcome. Imported cron jobs
must preserve their ledger and rewrite their origin and explicit destinations to
the runtime's single `squire:<conversation_id>` scope. Group jobs must live in the
separate approved group runtime, never the owner's private runtime.

DM and group runtimes have separate services, volumes, secrets, state, memory and
backup buckets. Groups expose only memory and the owner-approved action request
tool; a pre-tool hook rejects every other tool. Private action approval is checked
again immediately before execution and before each associated private tool.
Results stay private until the owner explicitly releases selected text.

## Boot and health

Required scoped variables and heartbeat contracts are documented in
`apps/control-api/README.md`. The ingress shim binds one explicit dual-stack
socket, supporting Railway's IPv4 health checks and IPv6 private networking.
Storage/ingress must become ready without a model credential; the adapter reports
`ready=true, model_ready=false` until a provider is configured. Model readiness
means configured, not that an external inference has succeeded. Imported founder
DM provider credentials and base URL remain unchanged.

The image contract gate checks the actual installed Hermes API, managed config
precedence, plugin discovery, platform enablement, session identity and group
tool veto. The adapter tests use the exact upstream source, not a replacement
Hermes implementation:

```
HERMES_SOURCE=/path/to/pinned/hermes python -m pytest tenant-image/overlay/tests
python tenant-image/patches/compatibility.py /path/to/pinned/hermes
```

The HTTP pipeline test runs the real ingress shim, adapter and an HTTP broker
fixture. It proves durable acceptance/replay and scoped requests; it does not
claim live WhatsApp delivery. Founder cutover additionally requires a successful
restore into the actual image, preserved sessions, scheduler/onboarding/connect,
browser guardrails, and private/group live round trips.

## Attachments

Inbound attachments are downloaded by event ID through the runtime's broker
capability, capped at 10 MiB and 24 hours. No caller-selected URL, provider address
or source filesystem path reaches the runtime. The adapter writes a private,
hashed cache file and populates Hermes' actual photo/audio/voice/video/document
fields. Cache files older than 24 hours are pruned on subsequent downloads.
Outbound local attachments pass Hermes' media path policy, then their bytes and
safe basename are durably queued with the same fixed recipient capability.
Image, audio, video and document methods support up to 10 MiB; Ogg audio may be
sent as a voice note. Captions are bounded to 32,000 characters. Remote image URLs
retain Hermes' text-link fallback; the adapter does not fetch arbitrary URLs.
