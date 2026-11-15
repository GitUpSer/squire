# Founder pilot migration

These tools prepare and verify an encrypted rehearsal without changing the
existing agent. Source cutover is a separate, gated operation. Do not run
`fence`, `rollback`, or `advance` as part of an initial snapshot.

## Verified source inventory

The 2026-11-15 read-only inventory found:

- Current release: `~/.hermes/releases/v2026.9.24__be3f3d797`, through
  `~/.hermes/hermes-agent`. Do not restore this state into the old 8.3 image.
- `~/.hermes/state.db`: approximately 1.50 GB, schema fingerprint
  `5bfa84adc1bfd8cbf5427a830707c82e2f63adcaf42ac3f9a51cdb5fb905cfbf`.
  Kanban, shared-state, verification, and scheduler files also need preservation.
- Hindsight's **live** PostgreSQL 18 instance:
  `~/.pg0/instances/hindsight-embed-hermes/data`, port 5432, socket `/tmp`,
  binaries `~/.pg0/installation/18.1.0/bin`. The other `hindsight/data`
  postmaster marker was unverified and must not be assumed active.
- `~/.hindsight` is approximately 9 MB; it does not contain all Hindsight
  state. Preserve its configuration plus a logical dump of the live PostgreSQL
  database. Never archive a running raw PostgreSQL data directory as a backup.
- Source units: `hermes-gateway.service`, `hermes-stall-recovery.service`
  (independent watchdog/recovery API), `hindsight-daemon.service`,
  `hindsight-healthcheck.timer`, and `camofox-server.service`.
- Source free space was approximately 10.5 GB. The initial bounded inventory
  hit its 10,000-file limit. It is a discovery report, not a complete backup
  manifest. `make-plan` enumerates every selected file or fails its limit.

`infra/pilot_inventory.py` emits table/column schemas, file counts, credential
**key names**, process-verified PostgreSQL identity and service states. It
never emits message rows, passwords, token values or environment values.
`infra/pilot_postgres_probe.py` discovers connection configuration without
printing credentials. It can use an existing daemon URL only inside child
process environment variables; no password is placed in command arguments.

## Encryption and backup destination

Install the checksum-verified `age` executable on the source. Only the public
recipient goes to source or production. The receiver's private identity stays
on the operator's Linux filesystem with mode 0600, outside the repository and
outside every production service. Preserve an encrypted recovery copy through
the operator's separate key-custody process. Losing this key loses all backups.

Source snapshots stream a tar archive into age; no plaintext tar is written.
The temporary staging directory is 0700 and individual files 0600. SQLite
and PostgreSQL temporary copies exist there during encryption, then the tool
removes its own staging directory. The source already contains readable data;
age protects transfer/backup storage, not a compromised live host.

The dedicated Railway bucket is `pilot-encrypted-backups`. Upload only `.age`
ciphertext. The `upload` command accepts `BACKUP_S3_ENDPOINT`,
`BACKUP_S3_BUCKET`, `BACKUP_S3_REGION`, `BACKUP_S3_ACCESS_KEY_ID` and
`BACKUP_S3_SECRET_ACCESS_KEY` in the environment. Values can be Railway service
references `${{pilot-encrypted-backups.ENDPOINT}}`, etc. Install `boto3` in the
operator environment for this optional command. Its object keys contain only
migration ID and ciphertext SHA-256. Download and decrypt a stored object to
prove recoverability; an upload/HEAD check is insufficient.

Enforce 30-day archive expiry in the backup worker/bucket lifecycle. The upload
metadata records this requirement but does not itself delete objects. Preserve
the original source machine and matching release for **at least 14 days after
successful cutover**. These tools never delete source state.

## Rehearsal commands

Run on the source as its dedicated `hermes` user. Keep plans and output in a
private directory outside `.hermes`, so a backup cannot include itself.

1. Save `pilot_inventory.py` output privately. Verify source version, schemas,
   active PostgreSQL identity and available space. Inspect the scheduler,
   crontab and external integration configuration locally without exposing
   credentials or customer content.
2. Create `postgres-config.json` containing `data_dir`, `binary_dir`, `host`,
   `port`, `user`, `database`, and optional `password_env`. Determine these from
   the live instance/daemon configuration; do not guess a default database.
   The password stays in the named environment variable, never this JSON.
3. Create a complete explicit file plan:

   ```sh
   python3 migrate_personal_agent.py make-plan --home /home/hermes \
     --postgres-config /private/postgres-config.json --output /private/snapshot-plan.json
   ```

   The plan preserves ordinary `.hermes` and `.hindsight` files, detects
   SQLite by its header, excludes release/cache/log/backup trees, and records
   omitted symlinks for review. Review all omissions for custom skills,
   plugins, scripts, workflows, browser state, jobs and external credentials.
   The PostgreSQL dump is always separate. A plan is private metadata and
   should not be committed to the repository.
4. Create the encrypted rehearsal snapshot:

   ```sh
   python3 migrate_personal_agent.py snapshot --home /home/hermes \
     --plan /private/snapshot-plan.json --recipient AGE_PUBLIC_RECIPIENT \
     --output /private/founder-rehearsal.age
   ```

   SQLite uses the backup API and integrity/schema checks. The tool verifies
   that `pg_dump` is major 18 and that `psql` connected to the **expected live
   data directory**, then creates and checks a custom-format logical dump.
   A rehearsal is transactionally consistent **per database**, not an atomic
   snapshot across SQLite, PostgreSQL and changing files. A changed ordinary
   file makes the snapshot fail; retry after it becomes quiet.

   Use the actual gateway interpreter for this source:
   `/home/hermes/.hermes/hermes-agent/venv/bin/python` supplies SQLite 3.50.4.
   System Python's SQLite 3.45.1 incorrectly reports integrity errors against
   this newer schema. The verified source's seven databases pass full integrity
   checks with 3.50.4; do not repair or downgrade them. Restore verification and
   the target image require a compatible SQLite build as well.
5. Transfer the `.age` file, compare ciphertext SHA-256, and decrypt on a
   private operator machine into a new quarantine directory:

   ```sh
   python3 infra/migrate_personal_agent.py decrypt --archive founder-rehearsal.age \
     --identity /private/operator-backup.agekey --sha256 EXPECTED_SHA256 \
     --destination /private/founder-rehearsal-restored
   ```

   Extraction rejects traversal, links, duplicates and excessive size. The
   full file manifest, every checksum, SQLite schema and integrity are checked.
   A failed age authentication leaves a `RESTORE_FAILED.json` quarantine marker
   and can never pass `verify`. Do not start a runtime from this directory.
6. Rehearse PostgreSQL restore into an **isolated empty** major-18 instance,
   using the original extensions/schema. `pg_restore --list` only validates
   the archive, not restore parity. Compare Hindsight bank counts and sample
   owner-approved recall behavior inside the restored image.

### External integration add-on

The core artifact does not contain every home-directory integration. Run
`pilot_integration_probe.py` for a bounded metadata-only inventory, then
`pilot_integration_backup.py plan --output PRIVATE_PLAN` and review its explicit
allowlist. The add-on includes existing `.camofox`, `.config`, `.codex`, `.claude`,
`.ssh`, `.aws`, `.netrc` and `.gitconfig`, skipping cache trees and symlinks.
It uses SQLite backup for any detected database and checks ordinary files for
concurrent mutation. Create it with `snapshot --output PRIVATE_ADDON.age
--recipient PUBLIC_RECIPIENT --migration-id UNIQUE_ID`. Decrypt and verify with
the same core `decrypt` command into a separate quarantine directory.

These files may contain browser cookies and tool credentials. They belong only
to the founder private runtime, never a group runtime or another customer.
Import each integration explicitly; do not restore the old systemd services,
source host `authorized_keys`, or old agent credentials wholesale. The source
`.hermes/scripts/data` link already points to an absent
`/home/hermes/kalshi-bot/data`; it is recorded as an omission, not fabricated.

The source Hindsight packages are API 0.5.0 / slim 0.5.2 with PostgreSQL 18.1,
vector 0.8.1 and Alembic revision `h3i4j5k6l7m8`. The target's Hindsight 0.9.0
requires a rehearsed forward schema upgrade and recall comparison before
the memory-parity gate can pass.

### Daily production backups

`infra/backup-worker.Dockerfile` runs `pilot_backup_worker.py daily` under a
daily Railway cron. `BACKUP_POSTGRES_DATABASES_JSON` maps `control`, `messaging`
and `litellm` to their database URL environment-variable names. The worker
creates PostgreSQL 18 custom dumps, age-encrypts them to
`BACKUP_AGE_RECIPIENT`, uploads via `BACKUP_S3_*`, downloads each ciphertext and
verifies its SHA-256. It holds no age private key. After a fully successful run
it removes managed `ops/<service>/` and `founder/` archives older than 30 days.

Once transport is paired, set worker `BACKUP_TRANSPORT_URL` and
`BACKUP_TRANSPORT_TOKEN`, matching transport's dedicated
`TRANSPORT_BACKUP_TOKEN` (never the operator token). Transport pauses its sync
writer and capture while taking consistent SQLite backups of account/session
and ledger, then returns only an age-encrypted archive. Restore it with outbound
disabled until the old writer is fenced. The worker's `restore-verify` command
downloads, decrypts and checks every manifest member; follow this with actual
isolated PostgreSQL restores, since `pg_restore --list` alone is insufficient.

Each tenant runtime has its own bucket and restic credentials. The runtime's
`squire-backup-prepare.py` takes online SQLite copies and a validated PostgreSQL
18 logical dump before restic runs. Raw live SQLite/WAL and `.pg0` cluster files
are excluded; a failed logical snapshot aborts the backup. Pilot retention uses
`SQUIRE_BACKUP_KEEP_WITHIN=30d`. Shared bucket-wide credentials must never enter
owner-accessible tenant runtimes.

## Conversation isolation and exact routing

Private import preserves the selected founder DM session IDs and message
history while changing their routing identity to platform `squire`, opaque
conversation ID, and key `agent:main:squire:dm:<conversation_id>`. It updates
the **primary** `state.db.gateway_routing` index, sessions source/origin/key
columns, and the legacy `sessions/sessions.json` mirror. Pass the final
runtime's absolute sessions directory as routing scope, not the temporary
restore directory. This follows the pinned 9.24 `SessionPersistenceMixin`.

Each mapping includes `old_session_id`, `old_chat_jid`, `kind:"dm"`,
`conversation_id`, and optional opaque `actor_id`. Verify mapping against
the messaging binding, not a display name. The importer checks the exact
inventoried schema and old source/chat identity; any change requires a new
reviewed schema adapter. Run on a disposable **verified restored copy**:

```sh
python3 infra/migrate_personal_agent.py remap-private --execute \
  --root /private/founder-rehearsal-restored --mapping /private/dm-map.json \
  --runtime-sessions-dir /data/.hermes/sessions
```

Unselected sessions/messages are removed from this copied database. Pending
delivery obligations, leases, gateway heartbeats, old routing, and in-flight
delegations are cleared, not replayed as new work. All originals remain in the
encrypted snapshot. Old raw transcript files, cron files, other per-profile
stores and `.env`/config must be reviewed before promotion: the database remap
does not authorize copying unrelated transcript files or enabling old WhatsApp
credentials in a new runtime. Disable the old direct WhatsApp adapter and old
scheduler before any restored runtime starts; translate each scheduled action
to an authorized Squire conversation grant and dedup key.

Group runtimes **never receive the founder snapshot**, model credentials,
private memories, tools, internal reasoning or personal Hindsight database.
`export-group` accepts one exact group session/chat mapping and writes an
independently encrypted artifact containing only user/assistant messages with
a persisted platform message ID. Missing visibility evidence is quarantined.
This intentionally omits internal/tool/system rows and is not a claim of full
media or complete assistant-history parity. Import the resulting visible
history through the isolated group's runtime, after current membership and
admin authorization. Group keys are `agent:main:squire:group:<conversation_id>`.

## Final cutover and rollback

Create a journal with the immutable target image digest (`sha256:...`).
Every `record-gate` report must contain matching `migration_id`,
`target_image_digest`, `gate`, and `passed:true`. Reports are operator evidence,
not assertions the tool manufactures. Required gates are:

- Restored-image compatibility and private-memory parity.
- Identity mapping and group isolation, including no private-state copy.
- Scheduler translation, with the exact source crontab SHA-256.
- Media parity, first-contact send/receipt, group roundtrip and rollback rehearsal.

Current media parity/short disappearing-message TTL and external integration
parity are not established by these tools. Missing evidence must keep the
source live and prevent final cutover.

After all gates, `fence --execute` records an in-progress journal, preserves
the exact crontab, replaces it with an inactive marker, and installs persistent
negative `ConditionPathExists` drop-ins on the source gateway, independent
watchdog, healthcheck timer, Hindsight and browser service. It stops and verifies
all five units. Originals remain intact, and reboot/manual timer starts cannot
restart the fenced units. The scheduler gate must also inventory detached
workers/non-systemd schedulers; an unaccounted writer invalidates the gate.

Take a new `snapshot --final --journal ...` after this fence. This snapshot is
labelled `writers_fenced`. Repeat encrypted transfer, restore and private/group
imports against the final artifact. Record `final_restore`, then
`advance --stage target_imported --execute`. Configure transport/messaging
generation consistently, activate the new binding/writer, and record fresh
`new_writer_healthy` evidence before `advance --stage active --execute`.
Record initial capture checkpoints and any in-flight work disposition in the
private cutover evidence. Never have both old and new senders active.

Rollback first pauses/revokes the new messaging bindings, scheduled grants,
transport generation and runtime writer. Record fresh `target_fenced` evidence
(at most five minutes old), then run `rollback --execute`. It removes only
its own fence drop-ins/marker, restores the exact saved crontab and starts only
units that were originally active. It never restores an old snapshot over the
preserved original machine. A partial fence remains journalled as `fencing`
and can be retried or rolled back; it does not silently unfreeze on error.
Do not delete the source or its matching release during the 14-day rollback
window. Account-wide sender and source service recovery require a deliberate
operator decision; no automatic rollback starts a second writer.

## Validation

```sh
SQUIRE_TEST_AGE_DIR=/path/to/verified/age/bin \
  python3 -m pytest infra/test_migrate_personal_agent.py -q
```

Synthetic tests cover WAL-safe committed snapshots, schema drift, path escape,
encrypted roundtrip/tamper quarantine, gate identity, private routing/session
preservation, stale-work removal, and group export excluding private memory,
tool output and credentials. They do not substitute for source snapshot restore,
live compatibility, real PostgreSQL restore, media/integration parity or the
founder/second-customer WhatsApp acceptance tests.
