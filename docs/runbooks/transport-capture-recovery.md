# Capture-only pairing and recovery

Keep `TRANSPORT_OUTBOUND_ENABLED=false` and `MESSAGING_OUTBOUND_ENABLED=false`
through this procedure. The original sender remains authoritative until its
separate cutover fence passes. Pairing does not authorize a second sender.

1. Pair privately with `TRANSPORT_PAIRING_MODE=true`. Never collect QR codes,
   phone codes, raw CLI output, account identifiers, or session credentials in
   task logs. After the pairing command exits successfully, set pairing mode
   false, sync enabled true, and outbound false. Temporarily disable retention
   until the first activated snapshot has passed restore verification.
2. Run `infra/pilot_transport_ops.py probe` inside the transport container via
   SSH stdin. This uses read-only `wacli doctor` without `--connect`, so the probe
   opens no additional WhatsApp connection. Require authenticated, not revoked,
   connected, exactly one sync process, and no store/capture error.
3. Run `activate --execute` using the same helper. It refuses an enabled sender
   and establishes the initial remote checkpoint without replaying paired
   history as new work. Require capture enabled and checkpoint committed.
   Connected status and a checkpoint do not prove a new live message roundtrip.
4. Run `backup --execute --output /tmp/<unique>.age` inside the container. It
   calls the dedicated backup-token endpoint, which pauses sync/capture while
   creating consistent SQLite copies and encrypting them. Transfer only the
   ciphertext to a mode-0700 operator directory. Keep the age private identity
   on the operator machine; the service receives only the public recipient.
5. Offline, use `restore-layout --archive <ciphertext> --identity <local-key>
   --sha256 <recorded-sha256> --destination <new-directory> --generation <N>`.
   N must exceed the snapshot generation. This authenticates/decrypts the
   archive, verifies every file and SQLite integrity, checks ledger generation,
   disables outbound in the new copy, and converts interrupted sends to
   uncertain. It does not launch a socket, CLI, gateway, or restored account.
   Snapshot databases must be standalone: unexpected WAL/SHM files fail
   validation. Never ignore that failure or declare such an archive restored.
6. Upload with `pilot_backup_worker.upload_and_roundtrip`; require the actual
   downloaded ciphertext hash to match. Store only checksums, counts and object
   keys in reports. Configure the daily worker's private `BACKUP_TRANSPORT_URL`
   and dedicated `BACKUP_TRANSPORT_TOKEN`; never substitute operator credentials.
7. Enable transport retention again and verify reconnect, one sync process,
   capture readiness and outbound disabled. The scheduled retention job uses
   acknowledged capture records and does not delete WhatsApp messages remotely.

An actual disaster recovery promotion requires stopping/fencing the previous
transport, selecting a new provider generation consistently in messaging and
transport, reconciling the remote ingest checkpoint against the restored local
journal, and keeping uncertain sends quarantined. An offline restore rehearsal
does not authorize that promotion. Preserve the original encrypted snapshot;
never run the offline session copy concurrently with the production connection.

## Selected group pilot

The operator's private selection record holds the user's chosen group, its
unique provider identity, and a current membership snapshot. For this pilot
test, the founder is a current administrator and member; the
linked agent is also a member. This selection records consent, but does not
create a messaging binding or authorize sending before the release gates pass.
Refresh membership at claim and delivery time; do not treat the saved snapshot
as a permanent administrator grant.

1. Finish the founder's verified DM binding and active sponsor entitlement.
   Keep outbound disabled while preparing the group. Fence the old sender for
   this conversation before the first live group interaction, or complete the
   single-writer cutover; otherwise the original gateway may also answer.
2. Once ready to use it, create a short-lived claim with authenticated
   control API `POST /v1/groups/claims`, or the service-authenticated messaging
   `POST /v1/control/groups/claims` with the sponsor tenant and a stable operation
   ID.
   Keep the returned nonce private. Claims expire after ten minutes by default,
   so do not generate one during a lengthy image build.
3. The founder posts the returned `!squire claim <nonce>` in the selected group.
   This is the existing in-band identity/administrator proof, not a new consent
   question. Because the founder is already an administrator, no separate
   `!squire approve` message is needed. Require the real captured event to
   originate in the privately selected group; do not manufacture an ingest
   event or manually insert an activated claim into the database.
4. Messaging verifies current owner membership and administrator identity,
   creates an addressed-mode group binding, and emits the durable
   `activate_group` control command. Control provisions a distinct sponsored
   group runtime with its own service, volume, conversation, scoped tokens and
   backup bucket. Use a fresh group home and standard group image, with private
   tools disabled. Do not copy the founder's DM memory, browser sessions,
   credentials, Hindsight bank, or private cron jobs. Importing historical group
   messages is a separate explicit scope decision, unnecessary for this test.
5. Require the new runtime's matching-generation readiness/model heartbeat,
   messaging registration, sponsor model authorization and recipient binding
   before enabling the coordinated sender. Confirm there is no unrelated
   queued outbound work eligible to drain when the global switch is enabled.
6. In the selected group, the founder sends one bounded prompt such as
   `!squire Reply with exactly SQUIRE_GROUP_OK and do not use tools.` Require
   one live captured event, one scoped runtime turn, one accepted outbound
   operation and one visible reply in that same group. Report IDs only in the
   private proof artifact; shared status should contain counts and outcomes.
   Retrying the identical event must not produce a second reply. An ordinary
   message without the prefix/reply context must not start another turn in
   addressed mode. Native WhatsApp mention routing remains unsupported by the
   pinned CLI; use `!squire` or a reply to the agent.

No group was joined, created, modified, claimed, provisioned or messaged by the
read-only selection step. The existing user selection is sufficient consent;
the remaining claim command is a required protocol step in the implemented
approval path.
