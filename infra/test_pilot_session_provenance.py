import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from migrate_personal_agent import MigrationError, checksum
import pilot_session_provenance as provenance


def fixture(tmp_path, routes=None):
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,chat_type TEXT,session_key TEXT,origin_json TEXT,thread_id TEXT,user_id TEXT)")
        db.execute("INSERT INTO sessions VALUES('known','whatsapp','owner@lid','dm',NULL,NULL,NULL,'owner@lid')")
        for session in ("legacy", "ambiguous"):
            db.execute("INSERT INTO sessions(id,source,user_id) VALUES(?,'whatsapp','owner@lid')", (session,))
    default = {"session_id": "legacy", "platform": "whatsapp", "chat_id": "owner@lid", "chat_type": "dm"}
    metadata = [default] if routes is None else routes
    def escape(value):
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
    lines = ["COPY public.memory_units (id, text, metadata) FROM stdin;\n"]
    for i, item in enumerate(metadata):
        lines.append(f"m{i}\tPRIVATE CONTENT IGNORED\t{escape(json.dumps(item))}\n")
    lines += ["\\.\n", "COPY public.documents (id, original_text, metadata, retain_params) FROM stdin;\n",
              "d1\tPRIVATE DOCUMENT IGNORED\t{}\t" + escape(json.dumps({"context": "PRIVATE CONTEXT", "metadata": default})) + "\n", "\\.\n"]
    dump = tmp_path / "dump.sql"
    dump.write_text("".join(lines))
    executable = tmp_path / "pg_restore"
    executable.write_text("#!/usr/bin/env python3\nimport pathlib,sys\nsys.stdout.write(pathlib.Path(sys.argv[-1]).read_text())\n")
    executable.chmod(0o700)
    return state, dump, executable


def review_for(observed):
    return {"format": "squire-session-provenance-review-v1", "state_sha256": observed["state_sha256"],
            "dump_sha256": observed["dump_sha256"], "candidate_manifest_sha256": provenance.fingerprint(observed),
            "approved_dm_chat_id": "owner@lid", "session_ids": ["legacy"]}


def test_exact_metadata_hydrates_new_copy_only_and_leaves_actor_only_ambiguous(tmp_path):
    state, dump, pg = fixture(tmp_path)
    original = checksum(state)
    observed = provenance.inspect(state, dump, pg)
    assert set(observed["candidates"]) == {"legacy"}
    assert len(observed["candidates"]["legacy"]["evidence"]) == 2
    assert "PRIVATE" not in json.dumps(observed)
    output = tmp_path / "hydrated.db"
    result = provenance.hydrate(state, dump, pg, review_for(observed), output)
    assert checksum(state) == original and result["hydrated_sessions"] == 1
    assert not result["runtime_start_authorized"] and result["requires_private_history_remap"]
    assert output.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT chat_id,chat_type FROM sessions WHERE id='legacy'").fetchone() == ("owner@lid", "dm")
        assert db.execute("SELECT chat_id,chat_type FROM sessions WHERE id='ambiguous'").fetchone() == (None, None)
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT chat_id FROM sessions WHERE id='legacy'").fetchone() == (None,)


@pytest.mark.parametrize("bad_metadata", [
    {"session_id": "legacy", "platform": "whatsapp", "chat_id": "group@g.us", "chat_type": "group"},
    {"session_id": "legacy", "platform": "whatsapp", "chat_id": "other@lid", "chat_type": "dm"},
    {"session_id": "legacy", "platform": "whatsapp", "chat_id": "owner@lid", "chat_type": "dm", "thread_id": "thread"},
    {"session_id": "legacy", "chat_id": "owner@lid", "chat_type": "dm"},
])
def test_conflicting_group_thread_or_incomplete_metadata_rejected(tmp_path, bad_metadata):
    state, dump, pg = fixture(tmp_path, [bad_metadata])
    observed = provenance.inspect(state, dump, pg)
    with pytest.raises(MigrationError, match="evidence"):
        provenance.hydrate(state, dump, pg, review_for(observed), tmp_path / "output.db")
    assert not (tmp_path / "output.db").exists()


@pytest.mark.parametrize("mutation", ["dump", "state", "review", "actor_only", "existing_origin"])
def test_review_source_and_actor_only_gates(tmp_path, mutation):
    state, dump, pg = fixture(tmp_path)
    observed = provenance.inspect(state, dump, pg)
    review = review_for(observed)
    if mutation == "dump":
        dump.write_text(dump.read_text() + "-- changed\n")
    elif mutation == "state":
        with sqlite3.connect(state) as db:
            db.execute("UPDATE sessions SET user_id='different' WHERE id='legacy'")
    elif mutation == "review":
        review["candidate_manifest_sha256"] = "0" * 64
    elif mutation == "actor_only":
        review["session_ids"] = ["ambiguous"]
    else:
        with sqlite3.connect(state) as db:
            db.execute("UPDATE sessions SET origin_json='{}' WHERE id='legacy'")
        review = review_for(provenance.inspect(state, dump, pg))
    before = checksum(state)
    with pytest.raises(MigrationError):
        provenance.hydrate(state, dump, pg, review, tmp_path / "output.db")
    assert checksum(state) == before and not (tmp_path / "output.db").exists()


def test_copy_escaping_and_no_clobber(tmp_path):
    assert provenance.copy_value(r"a\tb\\c\012d") == "a\tb\\c\nd"
    assert provenance.copy_value(r"\N") is None
    state, dump, pg = fixture(tmp_path)
    review = review_for(provenance.inspect(state, dump, pg))
    with pytest.raises(MigrationError, match="new_output"):
        provenance.hydrate(state, dump, pg, review, state)


def test_cli_private_inspection_artifact_and_wal_rejection(tmp_path):
    state, dump, pg = fixture(tmp_path)
    output = tmp_path / "candidates.json"
    command = [sys.executable, provenance.__file__, "inspect", "--state", str(state), "--dump", str(dump),
               "--pg-restore", str(pg), "--output", str(output)]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {"candidate_sessions": 1, "untyped_sessions": 2}
    assert output.stat().st_mode & 0o777 == 0o600
    assert "owner@lid" not in result.stdout and "PRIVATE" not in output.read_text()
    Path(str(state) + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(MigrationError, match="checkpointed"):
        provenance.inspect(state, dump, pg)


def test_wal_source_hydration_publishes_checkpointed_database_and_correct_hash(tmp_path):
    state, dump, pg = fixture(tmp_path)
    db = sqlite3.connect(state)
    db.execute("PRAGMA journal_mode=WAL")
    db.close()
    observed = provenance.inspect(state, dump, pg)
    output = tmp_path / "wal-hydrated.db"
    result = provenance.hydrate(state, dump, pg, review_for(observed), output)
    assert result["output_state_sha256"] == checksum(output)
    assert result["output_state_sha256"] != result["source_state_sha256"]
    # Read the published file alone, without recovering any unpublished sidecar.
    with sqlite3.connect(output.resolve().as_uri() + "?immutable=1", uri=True) as db:
        assert db.execute("SELECT chat_id FROM sessions WHERE id='legacy'").fetchone() == ("owner@lid",)
    assert not list(tmp_path.glob(".provenance-*"))
