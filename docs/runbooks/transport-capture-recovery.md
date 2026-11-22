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
