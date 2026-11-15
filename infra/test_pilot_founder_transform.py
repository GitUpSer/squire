import json
import sqlite3
import shutil
from contextlib import closing
from pathlib import Path
import pytest
import pilot_founder_transform as transform
from migrate_personal_agent import MigrationError,checksum


def source(tmp_path):
    root=tmp_path/"source";root.mkdir()
    jobs=[{"id":"private-job","origin":{"platform":"whatsapp","chat_id":"owner@s.whatsapp.net"},"deliver":"origin","enabled":True,"state":"scheduled","prompt":"PRIVATE PROMPT SENTINEL","schedule":{"kind":"interval","minutes":60},"next_run_at":"2026-11-15T20:00:00Z"},{"id":"group-job","origin":{"platform":"whatsapp","chat_id":"group@g.us"},"deliver":"whatsapp:group@g.us","enabled":True,"state":"scheduled","prompt":"PRIVATE GROUP TOOL CONTEXT","schedule":{"kind":"interval","minutes":60},"next_run_at":"2026-11-15T20:00:00Z"}]
    (root/"jobs.json").write_text(json.dumps({"jobs":jobs,"updated_at":"now"}))
    with sqlite3.connect(root/"executions.db") as db:
        db.executescript("CREATE TABLE executions(id TEXT PRIMARY KEY,job_id TEXT,source TEXT,process_id TEXT,pid INTEGER,process_started_at INTEGER,status TEXT,claimed_at TEXT,started_at TEXT,finished_at TEXT,error TEXT,handoff_pending INTEGER,handoff_started_at REAL,delivery_outcome TEXT,scheduled_instant TEXT);CREATE TABLE cron_incidents(id TEXT PRIMARY KEY,job_id TEXT,error TEXT);")
        for jid in ("private-job","group-job"):
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("run-"+jid,jid,"scheduler","old-process",123,10,"running","now",None,None,"PRIVATE ERROR",1,1,None,"2026-11-15T20:00:00Z"))
    with sqlite3.connect(root/"deliveries.db") as db:
        db.executescript("CREATE TABLE deliveries(execution_id TEXT PRIMARY KEY,job_json TEXT,content TEXT,for_failure INTEGER,status TEXT,owner_process_id TEXT,owner_pid INTEGER,owner_started_at INTEGER,created_at TEXT,finished_at TEXT,error TEXT);CREATE TABLE delivery_tombstones(execution_id TEXT PRIMARY KEY,terminal_status TEXT,finished_at TEXT);")
        for job in jobs:db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?)",("run-"+job["id"],json.dumps(job),"PRIVATE DELIVERY",0,"pending","process",123,10,"now",None,None))
    with sqlite3.connect(root/"notepad.db") as db:
        db.execute("CREATE TABLE cron_notepad(job_id TEXT,key TEXT,value TEXT,updated_at TEXT,PRIMARY KEY(job_id,key))")
        for job in jobs:db.execute("INSERT INTO cron_notepad VALUES(?,?,?,?)",(job["id"],"state","PRIVATE MEMORY SENTINEL","now"))
    return root,jobs


def plan_for(source,jobs):
    plan=transform.cron_inventory(source)
    plan["runtimes"]=[{"runtime_id":"owner-runtime","conversation_id":"dm-opaque","kind":"dm","old_chat_jid":"owner@s.whatsapp.net","jobs":[{"id":jobs[0]["id"],"source_sha256":transform.fingerprint(jobs[0])}]},{"runtime_id":"group-runtime","conversation_id":"group-opaque","kind":"group","old_chat_jid":"group@g.us","approved_group_claim_id":"approved-claim","jobs":[{"id":jobs[1]["id"],"source_sha256":transform.fingerprint(jobs[1]),"group_replacement":{"name":"Shared reminder","prompt":"Remind this group of its shared schedule."}}]}]
    return plan


def test_cron_split_preserves_occurrence_fences_unknown_and_excludes_private_group_bytes(tmp_path):
    original,jobs=source(tmp_path);before=transform.cron_inventory(original);plan=plan_for(original,jobs)
    output=tmp_path/"output";transform.transform_cron(original,output,plan)
    assert transform.cron_inventory(original)==before
    for runtime,conversation in (("owner-runtime","dm-opaque"),("group-runtime","group-opaque")):
        cron=output/runtime/"cron";job=json.loads((cron/"jobs.json").read_text())["jobs"][0]
        assert not job["enabled"] and job["state"]=="paused" and job["fire_claim"] is None
        assert job["deliver"]=="squire:"+conversation and job["origin"]["chat_id"]==conversation
        with sqlite3.connect(cron/"executions.db") as db:
            assert db.execute("SELECT status,scheduled_instant,pid FROM executions").fetchone()==("unknown","2026-11-15T20:00:00Z",0)
    group=output/"group-runtime/cron"
    assert b"PRIVATE" not in b"".join(p.read_bytes() for p in group.iterdir() if p.is_file())
    with sqlite3.connect(group/"notepad.db") as db:assert db.execute("SELECT COUNT(*) FROM cron_notepad").fetchone()[0]==0
    with sqlite3.connect(group/"deliveries.db") as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]==0
        assert db.execute("SELECT terminal_status FROM delivery_tombstones").fetchone()[0]=="unknown"


def test_cron_rejects_changed_snapshot_cross_context_and_unreviewed_group(tmp_path):
    original,jobs=source(tmp_path);plan=plan_for(original,jobs)
    plan["runtimes"][1]["jobs"][0].pop("group_replacement")
    with pytest.raises(MigrationError,match="sanitized"):transform.transform_cron(original,tmp_path/"rejected",plan)
    assert not (tmp_path/"rejected").exists()
    plan=plan_for(original,jobs)
    (original/"jobs.json").write_text('{"jobs":[]}')
    with pytest.raises(MigrationError,match="snapshot_changed"):transform.transform_cron(original,tmp_path/"changed",plan)


def test_memory_config_preserves_bank_and_only_repoints_loopback(tmp_path):
    source=tmp_path/"config.json";source.write_text(json.dumps({"mode":"local_external","bank_id":"original-bank","api_url":"http://127.0.0.1:8888","recall_budget":"mid"}))
    target=tmp_path/"runtime/config.json";before=checksum(source)
    report=transform.prepare_memory_config(source,target,before)
    assert report["bank_identity_preserved"] and "original-bank" not in json.dumps(report)
    assert json.loads(target.read_text())=={"mode":"local_external","bank_id":"original-bank","api_url":"http://127.0.0.1:9177","recall_budget":"mid"}
    assert checksum(source)==before
    with pytest.raises(MigrationError,match="local_runtime"):transform.prepare_memory_config(source,tmp_path/"bad.json",before,"https://external.invalid")


def test_group_history_import_uses_actual_hermes_new_database_only_visible_rows(tmp_path,monkeypatch):
    pytest.importorskip("hermes_state")
    connect=sqlite3.connect
    def wal_output(path,*args,**kwargs):
        db=connect(path,*args,**kwargs)
        if ".squire-group-import-" in str(path) and not str(path).startswith("file:"):
            db.execute("PRAGMA journal_mode=WAL")
        return db
    monkeypatch.setattr(sqlite3,"connect",wal_output)
    original=tmp_path/"export";original.mkdir()
    payload={"conversation_id":"group-opaque","messages":[{"role":"user","content":"Visible group text","timestamp":12345,"platform_message_id":"provider-id"}]}
    (original/"visible-history.json").write_text(json.dumps(payload))
    manifest={"format":"squire-group-visible-history-v1","scope":"group","conversation_id":"group-opaque","contains_private_memory":False,"contains_credentials":False,"history_sha256":checksum(original/"visible-history.json"),"message_count":1}
    (original/"manifest.json").write_text(json.dumps(manifest))
    output=tmp_path/"group-runtime"
    result=transform.import_group_history(original,output,"group-opaque","approved-claim","/opt/data/sessions")
    assert result["message_count"]==1
    with sqlite3.connect(output/"state.db") as db:
        assert db.execute("SELECT source,chat_id,chat_type FROM sessions").fetchall()==[("squire","group-opaque","group")]
        assert db.execute("SELECT role,content,tool_calls,tool_call_id FROM messages").fetchall()==[("user","Visible group text",None,None)]
        assert db.execute("SELECT COUNT(*) FROM system_prompts").fetchone()[0]==0
        assert db.execute("SELECT session_key FROM gateway_routing").fetchone()[0]=="agent:main:squire:group:group-opaque"
    assert not (output/"auth.json").exists() and not (output/"memory").exists()
    standalone=tmp_path/"group-standalone.db";shutil.copyfile(output/"state.db",standalone)
    with closing(sqlite3.connect(f"file:{standalone}?mode=ro&immutable=1",uri=True)) as db:
        assert db.execute("SELECT session_key FROM gateway_routing").fetchone()[0]=="agent:main:squire:group:group-opaque"
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]==1
        assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)


def test_actual_hermes_scheduler_resolves_registered_group_toolset(tmp_path,monkeypatch):
    pytest.importorskip("hermes_cli")
    for key,value in {"HERMES_HOME":str(tmp_path),"SQUIRE_RUNTIME_ID":"runtime","SQUIRE_OWNER_TENANT_ID":"owner","SQUIRE_CONVERSATION_ID":"group","SQUIRE_RUNTIME_KIND":"group","SQUIRE_INGRESS_TOKEN":"i"*40,"SQUIRE_EGRESS_TOKEN":"e"*40,"SQUIRE_CONTROL_TOKEN":"c"*40,"SQUIRE_MESSAGING_URL":"http://messaging.railway.internal:8080","CONTROL_API_URL":"http://control.railway.internal:8080"}.items():monkeypatch.setenv(key,value)
    (tmp_path/"config.yaml").write_text("plugins:\n  enabled: [squire-overlay]\n")
    from hermes_cli.plugins import _reset_plugin_managers_for_tests,discover_plugins,get_plugin_toolsets
    _reset_plugin_managers_for_tests();discover_plugins(force=True)
    try:
        from tools.registry import registry
        from cron.scheduler import _resolve_cron_enabled_toolsets
        assert "squire-group" in {item[0] for item in get_plugin_toolsets()}
        assert registry.get_tool_names_for_toolset("squire-group")==["squire_request_private_action"]
        # no_mcp is an upstream scheduler sentinel, stripped after preventing MCP expansion.
        resolved=_resolve_cron_enabled_toolsets({"enabled_toolsets":["memory","squire-group","no_mcp"]},{"mcp_servers":{"private-server":{"enabled":True}}})
        assert resolved==["memory","squire-group"]
    finally:_reset_plugin_managers_for_tests()


def test_private_environment_quarantines_transport_and_old_control_capabilities(tmp_path):
    source=tmp_path/"old.env";source.write_text("ANTHROPIC_API_KEY=private-model\nWHATSAPP_TOKEN=legacy-transport\nTELEGRAM_BOT_TOKEN=legacy-bot\nINTERNAL_API_TOKEN=old-master\nSQUIRE_RUNTIME_ID=old-scope\nHOME=/home/hermes\nCALENDAR_API_KEY=owner-calendar\n")
    output=tmp_path/"prepared/.env"
    result=transform.prepare_private_environment(source,output,checksum(source))
    assert output.read_text()=="ANTHROPIC_API_KEY=private-model\nCALENDAR_API_KEY=owner-calendar\n"
    assert result["retained_variables"]==2 and "WHATSAPP_TOKEN" in result["quarantined_variable_names"]
    assert "private-model" not in json.dumps(result) and "legacy-transport" not in json.dumps(result)


@pytest.mark.parametrize("sectioned",[False,True])
def test_private_promotion_flattens_home_and_writes_reviewed_marker_last(tmp_path,sectioned):
    source=tmp_path/"snapshot";prepared=tmp_path/"prepared";source.mkdir();prepared.mkdir()
    originals={".hermes/SOUL.md":"Owner identity", ".hermes/plugins/hindsight/__init__.py":"# approved public memory provider", ".ssh/id_owner":"PRIVATE OWNER SSH KEY"}
    prepared_files={"config.yaml":"platforms:\n  squire:\n    enabled: true\n", "hindsight/config.json":'{"mode":"local_external","bank_id":"existing-bank"}'}
    entries=[]
    for root,values,label in ((source,originals,"snapshot"),(prepared,prepared_files,"prepared")):
        for name,value in values.items():
            section=("core" if name.startswith(".hermes/") else "integrations") if sectioned and label=="snapshot" else ""
            physical=section+"/"+name if section else name
            path=root/physical;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
            entries.append({"root":label,"source":physical,**({"snapshot_section":section} if section else {}),"destination":name.removeprefix(".hermes/") if label=="snapshot" else name,"sha256":checksum(path),"category":"credential" if name.startswith(".ssh/") else "memory" if label=="snapshot" else "runtime_config","reviewed_no_direct_transport":True})
    # Archive restore deliberately flattens permissions to600; reviewed original
    # metadata, not local chmod guesses, authorizes executable restoration.
    script_relative=("core/" if sectioned else "")+".hermes/scripts/report.py"
    script=source/script_relative;script.parent.mkdir();script.write_text("print('safe report')");script.chmod(0o600)
    mode_record={"sha256":checksum(script),"size":script.stat().st_size,"source_mode":0o755}
    metadata=prepared/"source-modes.json";metadata.write_text(json.dumps({"files":{".hermes/scripts/report.py":mode_record}}))
    entries.append({"root":"snapshot","source":script_relative,**({"snapshot_section":"core"} if sectioned else {}),"destination":"scripts/report.py","sha256":checksum(script),"category":"workspace","reviewed_no_direct_transport":True,"source_mode":0o755})
    # This old account-wide session is deliberately not in the positive file list.
    forbidden_relative=("core/" if sectioned else "")+".hermes/whatsapp/session.json"
    forbidden=source/forbidden_relative;forbidden.parent.mkdir();forbidden.write_text("legacy secret")
    plan={"kind":"dm","runtime_id":"founder-runtime","migration_id":"migration-one","image_ref":"ghcr.io/org/image@sha256:"+"a"*64,"files":entries,"source_modes":{"root":"prepared","path":"source-modes.json","sha256":checksum(metadata)}}
    output=tmp_path/"runtime";transform.promote_private_files(source,prepared,output,plan)
    assert (output/"SOUL.md").read_text()=="Owner identity"
    assert (output/"scripts/report.py").stat().st_mode&0o777==0o700
    assert script.stat().st_mode&0o777==0o600
    assert not (output/".hermes").exists() and not (output/"whatsapp").exists()
    marker=json.loads((output/".squire/restored-home.json").read_text())
    assert marker["format"]=="squire-restored-home-v1" and marker["runtime_id"]=="founder-runtime"
    assert marker["files"]["SOUL.md"]==checksum(output/"SOUL.md")
    assert (output/".ssh/id_owner").stat().st_mode&0o777==0o600
    assert marker["credential_protection"]=={"existing_envelope":[],"owner_only_mode0600":[".ssh/id_owner"]}
    import importlib.util
    validator_path=Path(__file__).resolve().parents[1]/"tenant-image/bin/squire-restored-home.py"
    spec=importlib.util.spec_from_file_location("restored_home_validator",validator_path)
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    env={"TENANT_ID":"founder-runtime","SQUIRE_RUNTIME_KIND":"dm","SQUIRE_HINDSIGHT_VARIANT":"founder","HERMES_SKIP_CONFIG_MIGRATION":"1","SQUIRE_IMAGE_REF":plan["image_ref"]}
    validator.verify(output,output/".squire",env)
    with pytest.raises(ValueError,match="identity mismatch"):validator.verify(output,output/".squire",{**env,"TENANT_ID":"another-owner"})
    forbidden_entry={"root":"snapshot","source":forbidden_relative,**({"snapshot_section":"core"} if sectioned else {}),"destination":"whatsapp/session.json","sha256":checksum(forbidden),"category":"credential","reviewed_no_direct_transport":True}
    with pytest.raises(MigrationError,match="quarantined"):transform.promote_private_files(source,prepared,tmp_path/"bad-runtime",{**plan,"files":[*entries,forbidden_entry]})
    assert not (tmp_path/"bad-runtime").exists()
    browser_relative=("integrations/" if sectioned else "")+".camofox/checkpoints/owner.json"
    browser=source/browser_relative;browser.parent.mkdir(parents=True);browser.write_text('{"cookies":[]}')
    browser_entry={"root":"snapshot","source":browser_relative,**({"snapshot_section":"integrations"} if sectioned else {}),"destination":".camofox/checkpoints/owner.json","sha256":checksum(browser),"category":"browser","reviewed_no_direct_transport":True}
    with pytest.raises(MigrationError,match="encrypted_state_import"):transform.promote_private_files(source,prepared,tmp_path/"bad-browser",{**plan,"files":[*entries,browser_entry]})
    entries[-1]["source_mode"]=0o744
    with pytest.raises(MigrationError,match="source_mode_review_mismatch"):transform.promote_private_files(source,prepared,tmp_path/"bad-mode",plan)


def test_runtime_config_preserves_identity_but_removes_legacy_transports_and_hooks(tmp_path):
    import yaml
    source=tmp_path/"old-config.yaml"
    original={"model":"owner-model","memory":{"provider":"hindsight"},"platforms":{"whatsapp":{"enabled":True},"telegram":{"enabled":True}},"plugins":{"enabled":["legacy-personal"]},"hooks":{"send":"legacy-direct-send.py"},"whatsapp":{"enabled":True},"mcp_servers":{"calendar":{"command":"calendar-cli"},"old-direct":{"command":"wacli send"}},"platform_toolsets":{"whatsapp":["terminal","browser"]}}
    source.write_text(yaml.safe_dump(original));target=tmp_path/"prepared/config.yaml"
    transform.prepare_runtime_config(source,target,checksum(source),"opaque-owner",["calendar"],["/opt/data/skills"])
    config=yaml.safe_load(target.read_text())
    assert config["model"]=="owner-model" and set(config["platforms"])=={"squire"}
    assert "whatsapp" not in config and config["hooks"]=={}
    assert config["plugins"]["enabled"]==["squire-overlay"]
    assert config["mcp_servers"]=={"calendar":{"command":"calendar-cli"}}
    assert config["platform_toolsets"]=={"squire":["terminal","browser"]}
    with pytest.raises(MigrationError,match="direct_send"):transform.prepare_runtime_config(source,tmp_path/"bad.yaml",checksum(source),"opaque-owner",["old-direct"])


def test_cron_wal_outputs_are_complete_without_sidecars(tmp_path,monkeypatch):
    original,jobs=source(tmp_path);plan=plan_for(original,jobs)
    connect=sqlite3.connect
    def wal_output(path,*args,**kwargs):
        db=connect(path,*args,**kwargs)
        if ".squire-cron-transform-" in str(path) and not str(path).startswith("file:"):
            db.execute("PRAGMA journal_mode=WAL")
        return db
    monkeypatch.setattr(sqlite3,"connect",wal_output)
    output=tmp_path/"cron-output";transform.transform_cron(original,output,plan)
    for runtime in ("owner-runtime","group-runtime"):
        expected={"executions.db":("executions",1),"deliveries.db":("delivery_tombstones",1),"notepad.db":("cron_notepad",1 if runtime=="owner-runtime" else 0)}
        for filename,(table,count) in expected.items():
            standalone=tmp_path/(runtime+"-"+filename);shutil.copyfile(output/runtime/"cron"/filename,standalone)
            with closing(connect(f"file:{standalone}?mode=ro&immutable=1",uri=True)) as db:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==count
                assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)
