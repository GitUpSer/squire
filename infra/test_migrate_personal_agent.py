import hashlib
import io
import importlib.util
import json
import os
import sqlite3
import shutil
from contextlib import closing
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import migrate_personal_agent as migration
from pilot_inventory import sqlite_inventory


def make_sqlite(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,content TEXT)")
        db.execute("INSERT INTO sessions VALUES ('one','private transcript sentinel')")
    return sqlite_inventory(path)["schema_sha256"]


def test_sqlite_backup_preserves_committed_state_and_rejects_schema_drift(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "backup.db"
    schema = make_sqlite(source)
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO sessions VALUES ('two','uncommitted')")
        migration.backup_sqlite(source, target, schema)
        with sqlite3.connect(target) as db:
            assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    with pytest.raises(migration.MigrationError, match="schema_changed"):
        migration.backup_sqlite(source, tmp_path / "wrong.db", "0" * 64)


def test_safe_path_rejects_escape_and_symlink(tmp_path):
    with pytest.raises(migration.MigrationError):
        migration.safe_path(tmp_path, "../escape")
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(migration.MigrationError):
        migration.safe_path(tmp_path, "link/file")


def test_postgres_credentials_are_source_local_env_only_and_identity_checked(tmp_path):
    source = tmp_path / ".pg0/instances/hindsight-embed-hermes"
    source.mkdir(parents=True)
    config = dict(data_dir=str(source / "data"), port=5432, user="hindsight", database="hindsight", host="127.0.0.1",
                  credential_source="instance_config")
    (source / "instance.json").write_text(json.dumps(dict(data_dir=config["data_dir"], port=5432, username="hindsight",
        database="hindsight", password="source-only-password-sentinel")))
    env = migration.pg_environment(config, tmp_path)
    assert env["PGPASSWORD"] == "source-only-password-sentinel"
    assert "source-only-password-sentinel" not in json.dumps(config)
    with pytest.raises(migration.MigrationError, match="identity_mismatch"):
        migration.pg_environment(dict(config, database="wrong-instance"), tmp_path)


def preflight_fields(gate):
    values = dict(source_crontab_sha256=hashlib.sha256(b"").hexdigest())
    if gate == "identity_map": values.update(target_writer_scope_sha256="c"*64, provider_generation=2)
    if gate == "group_isolation": values.update(selected_group_sha256="d"*64, private_state_excluded=True, scoped_runtime_verified=True)
    if gate == "media_parity": values.update(evidence_kind="offline_media_fixture", attachment_roundtrip_verified=True, voice_roundtrip_verified=True, ephemeral_ttl_enforced=True, no_live_account_send=True)
    if gate == "rollback_rehearsal": values.update(evidence_kind="isolated_rollback_rehearsal", target_fence_before_source_restart=True, restore_verified=True, source_production_unchanged=True)
    return values


def writer_fields(clock):
    return dict(checked_at=clock[0], target_writer_scope_sha256="c"*64, provider_generation=2,
                single_transport_owner=True, outbound_enabled=True, dm_roundtrip_verified=True)


def target_fence_fields(clock):
    return dict(checked_at=clock[0], target_writer_scope_sha256="c"*64, provider_generation=2,
                evidence_kind="live_target_fence", transport_outbound_disabled=True, messaging_outbound_disabled=True,
                bindings_revoked=True, runtime_writers_stopped=True, scheduled_grants_revoked=True)


def group_fields(tmp_path, journal, clock):
    proof = tmp_path / "fence-proof.json"
    proof.write_text(json.dumps(dict(format="squire-source-fence-v1", verified=True,
        migration_id=journal.state["migration_id"], target_image_digest=journal.state["target_image_digest"],
        source_fenced_at=journal.state["source_fenced_at"], checked_at=clock[0], units_stopped=list(migration.FENCE_UNITS),
        persistent_conditions_verified=True, crontab_fenced=True, detached_writer_count=0)))
    reference = dict(path=str(proof), sha256=migration.checksum(proof))
    return dict(evidence_kind="live_selected_group", group_sha256="d"*64, runtime_scope_sha256="e"*64,
                provider_event_sha256="f"*64, provider_reply_sha256="a"*64, started_at=clock[0], completed_at=clock[0],
                founder_admin_verified=True, agent_member_verified=True, isolated_runtime_verified=True,
                duplicate_replay_suppressed=True, accepted_send_count=1, visible_reply_count=1,
                source_fence_before=reference, source_fence_after=reference)


def runtime_fields(clock):
    return dict(group_sha256="d"*64, runtime_scope_sha256="e"*64, checked_at=clock[0], claim_activated=True,
                founder_admin_verified=True, agent_member_verified=True, isolated_runtime_verified=True,
                model_ready=True, matching_generation_heartbeat=True, outbound_disabled=True)


def test_journal_gates_fencing_and_target_safe_rollback(tmp_path):
    journal = migration.Journal.create(tmp_path / "journal.json", "migration-test", "sha256:" + "1" * 64)
    with pytest.raises(migration.MigrationError, match="gates_incomplete"):
        journal.transition("fencing")
    for gate in migration.GATES:
        proof = tmp_path / (gate + ".json")
        proof.write_text(json.dumps(dict(gate=gate, migration_id="migration-test", target_image_digest="sha256:" + "1" * 64,
                                        passed=True, **preflight_fields(gate))))
        journal.record_gate(gate, proof)
    journal.transition("fencing")
    journal.transition("source_fenced")
    with pytest.raises(migration.MigrationError, match="requires_target_fence"):
        journal.transition("rolled_back")
    assert json.loads(journal.path.read_text())["stage"] == "source_fenced"
    journal.close()


def test_gate_evidence_must_match_image_and_migration(tmp_path):
    journal = migration.Journal.create(tmp_path / "journal.json", "one", "sha256:" + "1" * 64)
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps(dict(gate="restored_image", passed=True, migration_id="two", target_image_digest="sha256:" + "1" * 64)))
    with pytest.raises(migration.MigrationError, match="evidence_mismatch"):
        journal.record_gate("restored_image", proof)
    journal.close()


def active_journal(tmp_path, monkeypatch, activate=True):
    clock = [100000.0]
    monkeypatch.setattr(migration.time, "time", lambda: clock[0])
    journal = migration.Journal.create(tmp_path / "journal.json", "customer-test", "sha256:" + "2" * 64)
    def record(gate, **fields):
        if gate == "target_fenced" and not fields: fields = target_fence_fields(clock)
        proof = tmp_path / (gate + ".json")
        proof.write_text(json.dumps(dict(gate=gate, migration_id="customer-test", target_image_digest="sha256:" + "2" * 64,
                                        passed=True, **fields)))
        journal.record_gate(gate, proof)
    # Fresh recipient evidence is deliberately absent during founder migration.
    assert "first_contact" not in migration.GATES
    for gate in migration.GATES:
        record(gate, **preflight_fields(gate))
    journal.transition("fencing")
    journal.transition("source_fenced")
    journal.transition("final_snapshot")
    record("final_restore")
    journal.transition("target_imported")
    if activate:
        record("group_runtime_ready", **runtime_fields(clock))
        record("group_roundtrip", **group_fields(tmp_path, journal, clock))
        record("new_writer_healthy", **writer_fields(clock))
        journal.transition("active")
    return journal, clock, record


def finish_soak(journal, clock, record):
    clock[0] = journal.state["active_at"] + 86400
    record("founder_soak", soak_started_at=journal.state["active_at"], soak_completed_at=clock[0],
           scheduled_job_succeeded=True, no_unexplained_duplicate_or_missing_messages=True, private_group_isolation=True)
    journal.transition("tester_ready")


def first_contact_fields(clock):
    return dict(evidence_kind="live_website_signup", fresh_recipient=True, verified_email=True, phone_consent=True,
                received_before_first_inbound=True, first_agent_reply_received=True, tester_account_sha256="a" * 64,
                provider_message_id_sha256="b" * 64, signup_started_at=clock[0], first_received_at=clock[0])


@pytest.mark.parametrize("field", ["scheduled_job_succeeded", "no_unexplained_duplicate_or_missing_messages", "private_group_isolation"])
def test_soak_duration_alone_does_not_establish_founder_readiness(tmp_path, monkeypatch, field):
    journal, clock, record = active_journal(tmp_path, monkeypatch)
    try:
        clock[0] += 86400
        fields = dict(soak_started_at=journal.state["active_at"], soak_completed_at=clock[0],
                      scheduled_job_succeeded=True, no_unexplained_duplicate_or_missing_messages=True, private_group_isolation=True)
        fields[field] = False
        with pytest.raises(migration.MigrationError, match="acceptance_incomplete"):
            record("founder_soak", **fields)
        assert "founder_soak" not in journal.state["gates"]
    finally:
        journal.close()


def test_founder_can_migrate_before_fresh_test_but_readiness_requires_real_24h_soak(tmp_path, monkeypatch):
    journal, clock, record = active_journal(tmp_path, monkeypatch)
    try:
        with pytest.raises(migration.MigrationError, match="soak_unverified"):
            journal.transition("tester_ready")
        with pytest.raises(migration.MigrationError, match="requires_tester_ready"):
            record("first_contact", **first_contact_fields(clock))
        with pytest.raises(migration.MigrationError, match="24_hours"):
            record("founder_soak", soak_started_at=clock[0] - 86400, soak_completed_at=clock[0],
                   scheduled_job_succeeded=True, no_unexplained_duplicate_or_missing_messages=True, private_group_isolation=True)
        clock[0] += 86399
        with pytest.raises(migration.MigrationError, match="24_hours"):
            record("founder_soak", soak_started_at=journal.state["active_at"], soak_completed_at=clock[0])
        assert "founder_soak" not in journal.state["gates"] and "first_contact" not in journal.state["gates"]
        finish_soak(journal, clock, record)
        with pytest.raises(migration.MigrationError, match="fresh_first_contact_unverified"):
            journal.transition("first_contact_verified")
        record("first_contact", **first_contact_fields(clock))
        journal.transition("first_contact_verified")
        assert json.loads(journal.path.read_text())["stage"] == "first_contact_verified"
    finally:
        journal.close()


@pytest.mark.parametrize("field,value", [
    ("evidence_kind", "synthetic"), ("fresh_recipient", False), ("received_before_first_inbound", False),
    ("verified_email", False), ("phone_consent", False), ("first_agent_reply_received", False),
    ("provider_message_id_sha256", ""), ("signup_started_at", 0), ("first_received_at", 999999999),
])
def test_first_contact_requires_fresh_live_website_delivery_after_soak(tmp_path, monkeypatch, field, value):
    journal, clock, record = active_journal(tmp_path, monkeypatch)
    try:
        finish_soak(journal, clock, record)
        fields = first_contact_fields(clock)
        fields[field] = value
        with pytest.raises(migration.MigrationError):
            record("first_contact", **fields)
        assert "first_contact" not in json.loads(journal.path.read_text())["gates"]
        with pytest.raises(migration.MigrationError, match="fresh_first_contact_unverified"):
            journal.transition("first_contact_verified")
    finally:
        journal.close()


@pytest.mark.parametrize("stage", ["tester_ready", "first_contact_verified"])
def test_post_soak_stages_preserve_target_fenced_rollback(tmp_path, monkeypatch, stage):
    journal, clock, record = active_journal(tmp_path, monkeypatch)
    try:
        finish_soak(journal, clock, record)
        if stage == "first_contact_verified":
            record("first_contact", **first_contact_fields(clock))
            journal.transition(stage)
        with pytest.raises(migration.MigrationError, match="requires_target_fence"):
            migration.rollback_source(tmp_path, journal)
        record("target_fenced")
        clock[0] += 301
        with pytest.raises(migration.MigrationError, match="evidence_stale"):
            migration.rollback_source(tmp_path, journal)
        record("target_fenced")
        marker = tmp_path / ".local/state/squire-migration/source-fenced"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"migration_id":journal.state["migration_id"]}))
        cron = tmp_path / "cron.backup"
        cron.write_bytes(b"")
        journal.state.update(source_marker=str(marker), source_crontab_backup=str(cron))
        calls = []
        monkeypatch.setattr(migration, "run", lambda argv, **kwargs: calls.append(argv))
        migration.rollback_source(tmp_path, journal)
        assert journal.state["stage"] == "rolled_back" and not marker.exists()
        assert ["crontab", "-"] in calls
    finally:
        journal.close()


@pytest.fixture
def age_identity(tmp_path, monkeypatch):
    binary = Path(os.environ.get("SQUIRE_TEST_AGE_DIR", "/tmp/squire-age/arm64/age"))
    if not (binary / "age").exists():
        pytest.skip("requires verified age executable")
    monkeypatch.setenv("PATH", str(binary) + ":" + os.environ["PATH"])
    identity = tmp_path / "synthetic.agekey"
    subprocess.run([str(binary / "age-keygen"), "-o", str(identity)], check=True, capture_output=True)
    public = subprocess.run([str(binary / "age-keygen"), "-y", str(identity)], check=True, capture_output=True, text=True).stdout.strip()
    return identity, public


def test_encrypted_archive_roundtrip_and_tamper_quarantine(tmp_path, age_identity):
    identity, recipient = age_identity
    stage = tmp_path / "stage"
    stage.mkdir()
    schema = make_sqlite(stage / "state.db")
    (stage / "secret.env").write_text("MODEL_SECRET=synthetic-private-key-sentinel")
    files = {path.name: dict(size=path.stat().st_size, sha256=migration.checksum(path)) for path in stage.iterdir()}
    migration.atomic_json(stage / "manifest.json", dict(format=migration.FORMAT, scope="founder_private", migration_id="test",
        source_release=migration.SOURCE_RELEASE, final=False, files=files, sqlite={"state.db": schema}))
    archive = tmp_path / "snapshot.age"
    migration.encrypt_tree(stage, archive, recipient)
    assert b"synthetic-private-key-sentinel" not in archive.read_bytes()
    result = migration.decrypt_archive(archive, identity, tmp_path / "restored", migration.checksum(archive))
    assert result["verified"] and result["file_count"] == 2
    (tmp_path / "restored/secret.env").write_text("tampered")
    with pytest.raises(migration.MigrationError, match="corrupt"):
        migration.verify_tree(tmp_path / "restored")
    raw = bytearray(archive.read_bytes())
    raw[-2] ^= 1
    archive.write_bytes(raw)
    with pytest.raises((migration.MigrationError, tarfile.ReadError)):
        migration.decrypt_archive(archive, identity, tmp_path / "bad", migration.checksum(archive))
    assert (tmp_path / "bad/RESTORE_FAILED.json").exists()


def test_private_remap_rejects_group_or_unknown_schema(tmp_path):
    root = tmp_path / "restore"
    (root / ".hermes").mkdir(parents=True)
    make_sqlite(root / ".hermes/state.db")
    (root / "manifest.json").write_text(json.dumps(dict(scope="founder_private", source_release=migration.SOURCE_RELEASE)))
    with pytest.raises(migration.MigrationError, match="unreviewed_state_schema"):
        migration.remap_private_state(root, [{"kind": "group"}], "/data/.hermes/sessions")


def make_migration_state(root, monkeypatch):
    (root / ".hermes").mkdir(parents=True)
    state = root / ".hermes/state.db"
    with sqlite3.connect(state) as db:
        db.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,chat_type TEXT,
          session_key TEXT,thread_id TEXT,origin_json TEXT,transport_profile TEXT,parent_session_id TEXT,
          system_prompt_hash TEXT,handoff_state TEXT,handoff_platform TEXT,handoff_error TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,platform_message_id TEXT);
        CREATE TABLE session_model_usage(session_id TEXT);
        CREATE TABLE system_prompts(hash TEXT,prompt TEXT);
        CREATE TABLE gateway_routing(scope TEXT,session_key TEXT,entry_json TEXT,updated_at REAL);
        """)
        for table in ("async_delegations", "compression_locks", "conversation_generations", "delivery_obligations",
                      "gateway_heartbeats", "gateway_hygiene_state", "session_turn_leases"):
            db.execute(f"CREATE TABLE {table}(id TEXT)")
            db.execute(f"INSERT INTO {table} VALUES ('stale-work')")
        db.execute("INSERT INTO sessions(id,source,chat_id,chat_type,system_prompt_hash) VALUES ('dm','whatsapp','123@s.whatsapp.net','dm','private')")
        db.execute("INSERT INTO sessions(id,source,chat_id,chat_type,system_prompt_hash) VALUES ('group','whatsapp','456@g.us','group','group')")
        db.execute("INSERT INTO system_prompts VALUES ('private','private memory sentinel')")
        db.execute("INSERT INTO system_prompts VALUES ('group','shared prompt')")
        db.execute("INSERT INTO messages VALUES (1,'dm','user','private transcript',1,'private-msg')")
        db.execute("INSERT INTO messages VALUES (2,'group','user','visible group message',2,'group-msg')")
        db.execute("INSERT INTO messages VALUES (3,'group','tool','private tool sentinel',3,NULL)")
    (root / "manifest.json").write_text(json.dumps(dict(scope="founder_private", source_release=migration.SOURCE_RELEASE, migration_id="test")))
    monkeypatch.setattr(migration, "STATE_SCHEMA", sqlite_inventory(state)["schema_sha256"])
    return state


def test_private_remap_preserves_session_id_and_removes_other_chat_and_pending_work(tmp_path, monkeypatch):
    root = tmp_path / "private"
    state = make_migration_state(root, monkeypatch)
    result = migration.remap_private_state(root, [{"old_session_id": "dm", "kind": "dm", "old_chat_jid": "123@s.whatsapp.net",
                                                  "conversation_id": "opaque-1"}], "/data/.hermes/sessions")
    assert result["private_sessions_remapped"] == 1
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT id,source,chat_id FROM sessions").fetchall() == [("dm", "squire", "opaque-1")]
        assert db.execute("SELECT session_id,content FROM messages").fetchall() == [("dm", "private transcript")]
        assert db.execute("SELECT count(*) FROM delivery_obligations").fetchone()[0] == 0
        route = db.execute("SELECT scope,session_key,entry_json FROM gateway_routing").fetchone()
        assert route[0] == "/data/.hermes/sessions"
        assert route[1] == "agent:main:squire:dm:opaque-1"
        assert json.loads(route[2])["session_id"] == "dm"
    mirror = json.loads((root / ".hermes/sessions/sessions.json").read_text())
    assert mirror[route[1]]["session_id"] == "dm"


def test_group_archive_excludes_private_memory_tool_results_and_credentials(tmp_path, monkeypatch, age_identity):
    root = tmp_path / "source"
    state = make_migration_state(root, monkeypatch)
    (root / ".hermes/secret.env").write_text("MODEL_KEY=private credential sentinel")
    identity, recipient = age_identity
    archive = tmp_path / "group.age"
    result = migration.export_group_history(state, dict(kind="group", old_session_id="group", old_chat_jid="456@g.us",
                                                       conversation_id="group-conversation"), archive, recipient)
    assert result["exported_visible_messages"] == 1
    decrypted = subprocess.run(["age", "--decrypt", "--identity", str(identity), str(archive)], capture_output=True, check=True).stdout
    assert b"private memory sentinel" not in decrypted
    assert b"private tool sentinel" not in decrypted
    assert b"private credential sentinel" not in decrypted
    with tarfile.open(fileobj=io.BytesIO(decrypted)) as tar:
        assert set(tar.getnames()) == {"manifest.json", "visible-history.json"}
        history = json.load(tar.extractfile("visible-history.json"))
        assert history["messages"][0]["content"] == "visible group message"


def test_private_remap_retains_all_owner_history_with_one_active_route(tmp_path, monkeypatch):
    root=tmp_path/"private";state=make_migration_state(root,monkeypatch)
    with sqlite3.connect(state) as db:
        db.execute("INSERT INTO sessions(id,source,chat_id,chat_type,parent_session_id) VALUES ('older','whatsapp','123@s.whatsapp.net','dm',NULL)")
        db.execute("UPDATE sessions SET parent_session_id='older' WHERE id='dm'")
        db.execute("INSERT INTO messages VALUES(4,'older','user','older owner history',0,'old-msg')")
    mapping=[dict(old_session_id="older",kind="dm",old_chat_jid="123@s.whatsapp.net",conversation_id="opaque-1",active=False),dict(old_session_id="dm",kind="dm",old_chat_jid="123@s.whatsapp.net",conversation_id="opaque-1",active=True)]
    migration.remap_private_state(root,mapping,"/opt/data/sessions")
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]==2
        assert db.execute("SELECT parent_session_id FROM sessions WHERE id='dm'").fetchone()[0]=="older"
        assert db.execute("SELECT COUNT(*) FROM gateway_routing").fetchone()[0]==1
        assert json.loads(db.execute("SELECT entry_json FROM gateway_routing").fetchone()[0])["session_id"]=="dm"
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]==2


def test_wal_remap_is_complete_in_standalone_database_before_hash(tmp_path,monkeypatch):
    root=tmp_path/"wal-private";state=make_migration_state(root,monkeypatch)
    with closing(sqlite3.connect(state)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    migration.remap_private_state(root,[dict(old_session_id="dm",kind="dm",old_chat_jid="123@s.whatsapp.net",conversation_id="opaque-dm")],"/opt/data/sessions")
    standalone=tmp_path/"standalone.db";shutil.copyfile(state,standalone)
    with closing(sqlite3.connect(f"file:{standalone}?mode=ro&immutable=1",uri=True)) as db:
        assert db.execute("SELECT id,source,chat_id FROM sessions").fetchall()==[("dm","squire","opaque-dm")]
        assert db.execute("SELECT scope FROM gateway_routing").fetchall()==[("/opt/data/sessions",)]
        assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)
    report=json.loads((root/"IMPORT_RESULT.json").read_text())
    assert report["state_sha256"]==migration.checksum(standalone)==migration.checksum(state)


def test_backup_wal_commits_are_in_standalone_copy(tmp_path):
    source=tmp_path/"source-wal.db";target=tmp_path/"backup.db";schema=make_sqlite(source)
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO sessions VALUES('wal-committed','committed in WAL')");writer.commit()
        writer.execute("INSERT INTO sessions VALUES('not-committed','must not be included')")
        migration.backup_sqlite(source,target,schema)
        standalone=tmp_path/"standalone-backup.db";shutil.copyfile(target,standalone)
        with closing(sqlite3.connect(f"file:{standalone}?mode=ro&immutable=1",uri=True)) as db:
            assert db.execute("SELECT id FROM sessions ORDER BY id").fetchall()==[("one",),("wal-committed",)]
            assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)


def test_offline_sqlite_refuses_output_with_busy_checkpoint(tmp_path):
    path=tmp_path/"blocked-checkpoint.db"
    with closing(sqlite3.connect(path)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE payload(value TEXT)")
        setup.execute("INSERT INTO payload VALUES('before')");setup.commit()
        setup.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with closing(sqlite3.connect(path)) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT value FROM payload").fetchone()==("before",)
            with pytest.raises(migration.MigrationError,match="checkpoint_busy"):
                with migration.offline_sqlite(path) as writer:
                    writer.execute("PRAGMA busy_timeout=1")
                    writer.execute("UPDATE payload SET value='after'")
            reader.rollback()


def test_one_fence_order_requires_live_group_only_after_import(tmp_path, monkeypatch):
    journal, clock, record = active_journal(tmp_path, monkeypatch, activate=False)
    try:
        assert 'group_roundtrip' not in migration.GATES
        assert journal.state['stage'] == 'target_imported'
        with pytest.raises(migration.MigrationError, match='live_acceptance'):
            record('new_writer_healthy', **writer_fields(clock))
        record('group_runtime_ready', **runtime_fields(clock))
        record('group_roundtrip', **group_fields(tmp_path, journal, clock))
        record('new_writer_healthy', **writer_fields(clock))
        clock[0] += 301
        with pytest.raises(migration.MigrationError, match='live_acceptance_stale'):
            journal.transition('active')
    finally: journal.close()


@pytest.mark.parametrize('field,value', [('group_sha256','0'*64),('runtime_scope_sha256','0'*64),
    ('accepted_send_count',2),('visible_reply_count',0),('duplicate_replay_suppressed',False),
    ('evidence_kind','synthetic'),('started_at',0),('completed_at',999999999)])
def test_group_roundtrip_rejects_wrong_scope_or_incomplete_live_proof(tmp_path, monkeypatch, field, value):
    journal, clock, record = active_journal(tmp_path, monkeypatch, activate=False)
    try:
        record('group_runtime_ready', **runtime_fields(clock))
        fields = group_fields(tmp_path,journal,clock); fields[field]=value
        with pytest.raises(migration.MigrationError): record('group_roundtrip',**fields)
        assert 'group_roundtrip' not in journal.state['gates']
    finally: journal.close()


def test_group_gate_requires_fresh_verified_source_artifact_and_invalidates_old_runtime(tmp_path, monkeypatch):
    journal, clock, record = active_journal(tmp_path, monkeypatch, activate=False)
    try:
        record('group_runtime_ready', **runtime_fields(clock))
        fields=group_fields(tmp_path,journal,clock)
        clock[0]+=301
        with pytest.raises(migration.MigrationError,match='source_fence_proof_stale'): record('group_roundtrip',**fields)
        fields=group_fields(tmp_path,journal,clock)
        record('group_roundtrip',**fields)
        record('group_runtime_ready',**runtime_fields(clock))
        assert 'group_roundtrip' not in journal.state['gates']
        fields['source_fence_before']['sha256']='0'*64
        with pytest.raises(migration.MigrationError,match='missing_or_changed'):record('group_roundtrip',**fields)
    finally:journal.close()


def test_live_gate_cannot_be_recorded_before_source_fence(tmp_path):
    journal=migration.Journal.create(tmp_path/'journal.json','test','sha256:'+'1'*64)
    proof=tmp_path/'proof.json'
    proof.write_text(json.dumps(dict(gate='group_roundtrip',migration_id='test',target_image_digest='sha256:'+'1'*64,passed=True)))
    try:
        with pytest.raises(migration.MigrationError,match='requires_target_imported'):journal.record_gate('group_roundtrip',proof)
    finally:journal.close()


def test_old_target_fence_report_cannot_be_refreshed_by_rerecording(tmp_path,monkeypatch):
    journal,clock,record=active_journal(tmp_path,monkeypatch)
    try:
        fields=target_fence_fields(clock)
        record('target_fenced',**fields)
        clock[0]+=301
        with pytest.raises(migration.MigrationError,match='evidence_stale'):record('target_fenced',**fields)
        with pytest.raises(migration.MigrationError,match='evidence_stale'):journal.transition('rolled_back')
    finally:journal.close()


def prepare_fence_files(home,journal):
    marker=home/'.local/state/squire-migration/source-fenced';marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({'migration_id':journal.state['migration_id']}))
    journal.state['source_marker']=str(marker)
    for unit in migration.FENCE_UNITS:
        path=home/'.config/systemd/user'/(unit+'.d')/'90-squire-migration-fence.conf'
        path.parent.mkdir(parents=True);path.write_text(f'[Unit]\nConditionPathExists=!{marker}\n')
    return marker


def test_source_probe_checks_real_fence_files_and_only_runs_read_commands(tmp_path,monkeypatch):
    journal,clock,record=active_journal(tmp_path,monkeypatch,activate=False)
    try:
        marker=prepare_fence_files(tmp_path,journal)
        calls=[]
        monkeypatch.setattr(migration,'detached_writer_pids',lambda:[])
        monkeypatch.setattr(migration,'run',lambda argv,**kw: calls.append(argv) or migration.FENCED_CRONTAB)
        monkeypatch.setattr(migration,'service_inventory',lambda:{unit:{'ActiveState':'inactive'} for unit in migration.FENCE_UNITS})
        result=migration.source_fence_report(tmp_path,journal,tmp_path/'fence-proof-generated.json')
        assert result['verified'] and calls==[['crontab','-l']]
        path=tmp_path/'.config/systemd/user'/(migration.FENCE_UNITS[0]+'.d')/'90-squire-migration-fence.conf'
        path.write_text('[Unit]\n')
        with pytest.raises(migration.MigrationError,match='condition_changed'):migration.source_fence_report(tmp_path,journal,tmp_path/'failed.json')
        assert marker.exists() and not (tmp_path/'failed.json').exists()
    finally:journal.close()


def test_rollback_checks_crontab_before_removing_any_fence(tmp_path,monkeypatch):
    journal,clock,record=active_journal(tmp_path,monkeypatch)
    try:
        record('target_fenced')
        marker=prepare_fence_files(tmp_path,journal)
        cron=tmp_path/'source-cron';cron.write_bytes(b'tampered')
        journal.state['source_crontab_backup']=str(cron)
        calls=[];monkeypatch.setattr(migration,'run',lambda argv,**kw:calls.append(argv))
        with pytest.raises(migration.MigrationError,match='crontab_backup_changed'):migration.rollback_source(tmp_path,journal)
        assert marker.exists() and not calls
        assert len(list((tmp_path/'.config').rglob('90-squire-migration-fence.conf')))==len(migration.FENCE_UNITS)
    finally:journal.close()


def test_live_group_activity_invalidates_previously_recorded_target_fence(tmp_path,monkeypatch):
    journal,clock,record=active_journal(tmp_path,monkeypatch,activate=False)
    try:
        old=target_fence_fields(clock);record('target_fenced',**old)
        clock[0]+=1
        record('group_runtime_ready',**runtime_fields(clock))
        record('group_roundtrip',**group_fields(tmp_path,journal,clock))
        assert 'target_fenced' not in journal.state['gates']
        with pytest.raises(migration.MigrationError,match='evidence_stale'):record('target_fenced',**old)
        with pytest.raises(migration.MigrationError,match='requires_target_fence'):journal.transition('rolled_back')
        record('target_fenced',**target_fence_fields(clock))
        journal.transition('rolled_back')
    finally:journal.close()


def test_detached_writer_scan_uses_identity_tokens_not_inspection_text(tmp_path):
    def process(pid,args):
        p=tmp_path/str(pid);p.mkdir();(p/'cmdline').write_bytes(b'\0'.join(x.encode() for x in args))
    process(99999001,['node','/source/bridge.js','--credentials','private-sentinel'])
    process(99999002,['bash','/source/.hermes/scripts/wa-watchdog.sh'])
    process(99999003,['python','-c','print("bridge.js wa-watchdog.sh")'])
    process(99999004,['postgres','-D','/source/pg0/data'])
    assert set(migration.detached_writer_pids(tmp_path))=={99999001,99999002}


def test_target_fence_invalidates_previously_healthy_activation_proofs(tmp_path,monkeypatch):
    journal,clock,record=active_journal(tmp_path,monkeypatch,activate=False)
    try:
        record('group_runtime_ready',**runtime_fields(clock))
        record('group_roundtrip',**group_fields(tmp_path,journal,clock))
        record('new_writer_healthy',**writer_fields(clock))
        record('target_fenced')
        assert not (migration.LIVE_GATES|{'new_writer_healthy'}) & journal.state['gates'].keys()
        with pytest.raises(migration.MigrationError,match='new_writer_unverified'):journal.transition('active')
    finally:journal.close()


@pytest.mark.parametrize('gate,field,value',[
    ('media_parity','evidence_kind','live_whatsapp_send'),('media_parity','ephemeral_ttl_enforced',False),
    ('rollback_rehearsal','evidence_kind','production_restart'),('rollback_rehearsal','source_production_unchanged',False)])
def test_preflight_media_and_rollback_cannot_require_live_sender_actions(tmp_path,gate,field,value):
    journal=migration.Journal.create(tmp_path/'journal.json','test','sha256:'+'1'*64)
    try:
        fields=preflight_fields(gate);fields[field]=value
        proof=tmp_path/'proof.json';proof.write_text(json.dumps(dict(gate=gate,migration_id='test',target_image_digest='sha256:'+'1'*64,passed=True,**fields)))
        with pytest.raises(migration.MigrationError,match='preflight_required'):journal.record_gate(gate,proof)
        assert gate not in journal.state['gates']
    finally:journal.close()


def checkout_link(tmp_path):
    checkout = tmp_path / 'repos/example'
    (checkout / '.claude/skills').mkdir(parents=True)
    (checkout / 'skills/discovery').mkdir(parents=True)
    descriptor = dict(path='repos/example/.claude/skills/discovery',
                      target='../../skills/discovery', checkout_root='repos/example')
    (tmp_path / descriptor['path']).symlink_to(descriptor['target'])
    return descriptor


def test_approved_checkout_link_is_descriptor_only_and_exact(tmp_path):
    item = checkout_link(tmp_path)
    assert migration.approved_symlinks(tmp_path, [item]) == [item]
    with pytest.raises(migration.MigrationError, match='symlink_not_permitted'):
        migration.safe_path(tmp_path, item['path'])
    with pytest.raises(migration.MigrationError, match='approved_symlink_changed'):
        migration.approved_symlinks(tmp_path, [dict(item, target='../../skills')])
    with pytest.raises(migration.MigrationError, match='invalid_approved_symlink'):
        migration.approved_symlinks(tmp_path, [item, item])


@pytest.mark.parametrize('target', ['/tmp', '../../../example/skills/discovery', '../../skills/link/../discovery',
                                    '../../skills/missing/../discovery'])
def test_approved_checkout_link_rejects_escape_and_symlink_traversal(tmp_path, target):
    item = checkout_link(tmp_path)
    link = tmp_path / item['path']
    link.unlink()
    link.symlink_to(target)
    (tmp_path / 'repos/example/skills/link').symlink_to('discovery')
    with pytest.raises(migration.MigrationError):
        migration.approved_symlinks(tmp_path, [dict(item, target=target)])


def snapshot_fixture(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.hermes/hermes-agent').mkdir(parents=True)
    (home / '.hermes/hermes-agent').rename(home / '.hermes' / migration.SOURCE_RELEASE)
    (home / '.hermes/hermes-agent').symlink_to(migration.SOURCE_RELEASE)
    source = home / 'job-output.txt'
    source.write_text('preserved output')
    source.chmod(0o640)
    os.utime(source, ns=(1234567890123456, 1234567890123456))
    schema = make_sqlite(home / 'state.db')
    plan = dict(source_release=migration.SOURCE_RELEASE, scope='founder_private', migration_id='fixture',
                files=['job-output.txt'], sqlite={'state.db': schema}, postgres={'fixture': True})
    monkeypatch.setattr(migration.shutil, 'which', lambda _: '/bin/true')
    monkeypatch.setattr(migration, 'dump_postgres', lambda _home, _cfg, dest: (dest.parent.mkdir(), dest.write_bytes(b'dump')))
    observed = {}
    def archive(stage, output, recipient):
        observed['manifest'] = json.loads((stage / 'manifest.json').read_text())
        observed['symlinks'] = [str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_symlink()]
        output.write_bytes(b'encrypted-fixture')
    monkeypatch.setattr(migration, 'encrypt_tree', archive)
    return home, source, plan, observed


def test_snapshot_authenticates_original_mtime_and_reviewed_link_without_tarlink(tmp_path, monkeypatch):
    home, source, plan, observed = snapshot_fixture(tmp_path, monkeypatch)
    plan['approved_symlinks'] = [checkout_link(home)]
    migration.snapshot(home, plan, tmp_path / 'snapshot.age', 'age1fixture')
    record = observed['manifest']['files']['job-output.txt']
    assert record['source_mtime_ns'] == 1234567890123456
    assert record['source_mode'] == 0o640
    assert record['sha256'] == migration.checksum(source)
    assert observed['manifest']['approved_symlinks'] == plan['approved_symlinks']
    assert not observed['symlinks']
    assert plan['approved_symlinks'][0]['path'] not in observed['manifest']['files']


@pytest.mark.parametrize('mutation', ['source_mode', 'source_content', 'symlink'])
def test_snapshot_rechecks_source_metadata_and_links_before_encryption(tmp_path, monkeypatch, mutation):
    home, source, plan, observed = snapshot_fixture(tmp_path, monkeypatch)
    plan['approved_symlinks'] = [checkout_link(home)]
    original_dump = migration.dump_postgres
    def mutate(*args):
        original_dump(*args)
        if mutation == 'source_mode': source.chmod(0o600)
        elif mutation == 'source_content': source.write_text('changed')
        else:
            link = home / plan['approved_symlinks'][0]['path']
            link.unlink()
            link.symlink_to('../../skills')
    monkeypatch.setattr(migration, 'dump_postgres', mutate)
    with pytest.raises(migration.MigrationError, match='changed'):
        migration.snapshot(home, plan, tmp_path / 'snapshot.age', 'age1fixture')
    assert not observed
