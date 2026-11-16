#!/usr/bin/env python3
"""Offline-only scoped founder transformations. Never starts a runtime or edits source."""
from __future__ import annotations
import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone
from migrate_personal_agent import MigrationError, require, atomic_json, checksum, SAFE_PART, offline_sqlite
from pilot_inventory import sqlite_inventory

DATABASES={"executions.db":("executions","cron_incidents"),"deliveries.db":("deliveries","delivery_tombstones"),"notepad.db":("cron_notepad",)}


def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


def cron_inventory(source):
    source=Path(source)
    jobs=json.loads((source/"jobs.json").read_text())["jobs"]
    return {"source_files":{name:checksum(source/name) for name in ("jobs.json",*DATABASES)},
            "source_schemas":{name:sqlite_inventory(source/name)["schema_sha256"] for name in DATABASES},
            "jobs":[{"id":job["id"],"source_sha256":fingerprint(job),"enabled":bool(job.get("enabled")),
                     "has_script":bool(job.get("script") or job.get("monitor_script")),
                     "origin_platform":(job.get("origin") or {}).get("platform")} for job in jobs]}


def empty_database(source,destination,tables):
    require(not destination.exists(),"destination_database_exists")
    with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro",uri=True)) as old, offline_sqlite(destination) as new:
        for table in tables:
            sql=old.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            require(sql is not None,"source_ledger_table_missing")
            new.execute(sql[0])
    destination.chmod(0o600)


def insert_row(db,table,row):
    columns=list(row)
    db.execute('INSERT INTO "'+table+'" ('+','.join('"'+key+'"' for key in columns)+') VALUES ('+','.join('?' for _ in columns)+')',[row[key] for key in columns])


def read_rows(path,table):
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM "'+table+'"')]


def translated_job(job,spec,runtime,selected):
    require(fingerprint(job)==spec["source_sha256"],"cron_job_changed_since_review")
    origin=job.get("origin") or {}
    if origin:
        require(origin.get("platform")=="whatsapp" and origin.get("chat_id")==runtime["old_chat_jid"],"cron_origin_scope_mismatch")
    else:require(runtime["kind"]=="dm" and spec.get("allow_unscoped_private") is True,"unscoped_job_requires_private_review")
    target="squire:"+runtime["conversation_id"]
    for lane in ("deliver","failure_deliver"):
        prior=job.get(lane)
        require(prior in (None,"local","origin","whatsapp","whatsapp:"+runtime["old_chat_jid"]),"multi_recipient_job_requires_manual_reauthoring")
    if runtime["kind"]=="group":
        replacement=spec.get("group_replacement")
        require(runtime.get("approved_group_claim_id") and isinstance(replacement,dict),"group_requires_approved_sanitized_replacement")
        require(set(replacement)=={"name","prompt"} and all(isinstance(replacement[k],str) and replacement[k].strip() for k in replacement),"invalid_group_replacement")
        # No source prompt, script, context_from, memory/notepad or model snapshot.
        result={key:copy.deepcopy(job[key]) for key in ("id","schedule","repeat","created_at","next_run_at") if key in job}
        result.update(replacement);result.update({"enabled_toolsets":["memory","squire-group","no_mcp"],"no_agent":False})
    else:
        result=copy.deepcopy(job)
        contexts=result.get("context_from") or []
        if isinstance(contexts,str):contexts=[contexts]
        require(set(contexts)<=selected,"cross_scope_cron_context")
        # Paths are explicit reviewed replacements, never string-substituted inside shell commands.
        for field in ("script","monitor_script","workdir"):
            if result.get(field):
                mapped=(spec.get("runtime_paths") or {}).get(field)
                require(isinstance(mapped,str) and Path(mapped).is_absolute(),"script_or_workdir_requires_runtime_path")
                result[field]=mapped
    result["origin"]={"platform":"squire","chat_id":runtime["conversation_id"],"chat_type":runtime["kind"],"thread_id":None,"user_id":runtime.get("actor_id","owner") if runtime["kind"]=="dm" else "group-scheduler"}
    result["deliver"]="local" if job.get("deliver")=="local" else target
    if job.get("failure_deliver") is not None:result["failure_deliver"]="local" if job["failure_deliver"]=="local" else target
    result.update({"enabled":False,"state":"paused","paused_reason":"squire_migration_review","paused_at":datetime.now(timezone.utc).isoformat(),"fire_claim":None})
    result.pop("last_delivery_unverified",None);result.pop("last_delivery_queued",None);result.pop("last_dispatch",None)
    return result


def transform_cron(source,output,plan):
    source,output=Path(source),Path(output)
    require(not output.exists(),"output_must_be_new_offline_directory")
    observed=cron_inventory(source)
    require(plan.get("source_files")==observed["source_files"] and plan.get("source_schemas")==observed["source_schemas"],"source_cron_snapshot_changed")
    jobs={job["id"]:job for job in json.loads((source/"jobs.json").read_text())["jobs"]}
    runtimes=plan.get("runtimes",[]);require(bool(runtimes),"runtime_mapping_required")
    require(len({r["runtime_id"] for r in runtimes})==len(runtimes),"duplicate_runtime_mapping")
    all_selected=[spec["id"] for runtime in runtimes for spec in runtime.get("jobs",[])]
    require(len(set(all_selected))==len(all_selected) and set(all_selected)<=set(jobs),"duplicate_or_unknown_job_assignment")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".squire-cron-transform-",dir=output.parent) as temporary:
        stage=Path(temporary);os.chmod(stage,0o700);reports=[]
        for runtime in runtimes:
            require(runtime.get("kind") in {"dm","group"} and SAFE_PART.fullmatch(runtime["runtime_id"]) and SAFE_PART.fullmatch(runtime["conversation_id"]),"invalid_runtime_mapping")
            selected={spec["id"] for spec in runtime.get("jobs",[])}
            translated={spec["id"]:translated_job(jobs[spec["id"]],spec,runtime,selected) for spec in runtime.get("jobs",[])}
            destination=stage/runtime["runtime_id"]/"cron";destination.mkdir(parents=True,mode=0o700)
            atomic_json(destination/"jobs.json",{"jobs":list(translated.values()),"updated_at":datetime.now(timezone.utc).isoformat()})
            for name,tables in DATABASES.items():empty_database(source/name,destination/name,tables)
            executions={row["id"]:row for row in read_rows(source/"executions.db","executions") if row["job_id"] in selected}
            uncertain=[]
            with offline_sqlite(destination/"executions.db") as db:
                for row in executions.values():
                    if row["status"] in {"claimed","running","unknown"}:
                        row["status"]="unknown";row["error"]="migration_requires_reconciliation";uncertain.append(row["id"])
                        row["handoff_pending"]=0;row["handoff_started_at"]=None
                    row["process_id"]="migration-fenced";row["pid"]=0;row["process_started_at"]=None
                    if runtime["kind"]=="group":row["error"]=None;row["source"]="migration"
                    insert_row(db,"executions",row)
                if runtime["kind"]=="dm":
                    for row in read_rows(source/"executions.db","cron_incidents"):
                        if row["job_id"] in selected:insert_row(db,"cron_incidents",row)
            with offline_sqlite(destination/"deliveries.db") as db:
                for row in read_rows(source/"deliveries.db","deliveries"):
                    if row["execution_id"] not in executions:continue
                    status=row["status"]
                    if status in {"pending","delivering","unknown"}:status="unknown";uncertain.append(row["execution_id"])
                    if runtime["kind"]=="dm":
                        row.update({"status":status,"job_json":json.dumps(translated[executions[row["execution_id"]]["job_id"]]),"owner_process_id":None,"owner_pid":None,"owner_started_at":None})
                        if status=="unknown":row["error"]="migration_requires_reconciliation"
                        insert_row(db,"deliveries",row)
                    # Group receives only receipts, never private job output text.
                    insert_row(db,"delivery_tombstones",{"execution_id":row["execution_id"],"terminal_status":status,"finished_at":row.get("finished_at")})
                existing={row[0] for row in db.execute("SELECT execution_id FROM delivery_tombstones")}
                for row in read_rows(source/"deliveries.db","delivery_tombstones"):
                    if row["execution_id"] in executions and row["execution_id"] not in existing:insert_row(db,"delivery_tombstones",row)
            if runtime["kind"]=="dm":
                with offline_sqlite(destination/"notepad.db") as db:
                    for row in read_rows(source/"notepad.db","cron_notepad"):
                        if row["job_id"] in selected:insert_row(db,"cron_notepad",row)
            reports.append({"runtime_id":runtime["runtime_id"],"kind":runtime["kind"],"jobs":len(selected),"job_ids":sorted(selected),"uncertain_execution_ids":sorted(set(uncertain)),"formerly_enabled_job_ids":[jid for jid in selected if jobs[jid].get("enabled")],"all_jobs_paused":True})
        require(cron_inventory(source)["source_files"]==observed["source_files"],"source_changed_during_transform")
        atomic_json(stage/"TRANSFORM_RESULT.json",{"format":"squire-scoped-cron-v1","source_files":observed["source_files"],"runtimes":reports,"unassigned_job_ids":sorted(set(jobs)-set(all_selected)),"runtime_start_authorized":False,"requires_explicit_future_next_run_and_reconciliation":True})
        os.replace(stage,output)
    return {"runtimes":len(reports),"jobs":len(all_selected),"runtime_start_authorized":False}


def prepare_memory_config(source,output,expected_checksum,target_api_url="http://127.0.0.1:9177"):
    from urllib.parse import urlsplit
    source,output=Path(source),Path(output)
    require(checksum(source)==expected_checksum,"source_memory_config_changed")
    require(not output.exists(),"memory_config_output_exists")
    parsed=urlsplit(target_api_url)
    require(parsed.scheme=="http" and parsed.hostname in {"127.0.0.1","localhost","::1"} and parsed.port==9177 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,"memory_endpoint_must_be_local_runtime")
    config=json.loads(source.read_text())
    require(config.get("mode")=="local_external" and isinstance(config.get("bank_id"),str) and bool(config["bank_id"]),"explicit_existing_bank_required")
    original_bank=config["bank_id"];config["api_url"]=target_api_url
    atomic_json(output,config)
    restored=json.loads(output.read_text())
    require(restored["bank_id"]==original_bank and checksum(source)==expected_checksum,"memory_bank_identity_changed")
    return {"bank_id_sha256":hashlib.sha256(original_bank.encode()).hexdigest(),"bank_identity_preserved":True,"source_config_sha256":expected_checksum,"target_config_sha256":checksum(output),"runtime_start_authorized":False}


def import_group_history(source,output,conversation_id,approved_claim_id,runtime_sessions_dir):
    """Create a NEW Hermes database from the visible-history export only."""
    from hermes_state import SessionDB
    import hermes_cli
    require(hermes_cli.__version__=="0.21.5","unreviewed_hermes_import_version")
    source,output=Path(source),Path(output)
    require(not output.exists() and SAFE_PART.fullmatch(conversation_id) and approved_claim_id and Path(runtime_sessions_dir).is_absolute(),"group_import_scope_required")
    manifest=json.loads((source/"manifest.json").read_text())
    require(manifest.get("format")=="squire-group-visible-history-v1" and manifest.get("scope")=="group" and manifest.get("conversation_id")==conversation_id,"group_export_identity_mismatch")
    require(manifest.get("contains_private_memory") is False and manifest.get("contains_credentials") is False and checksum(source/"visible-history.json")==manifest.get("history_sha256"),"group_export_integrity_failed")
    payload=json.loads((source/"visible-history.json").read_text())
    require(payload.get("conversation_id")==conversation_id and len(payload.get("messages",[]))==manifest.get("message_count"),"group_export_count_mismatch")
    for item in payload["messages"]:
        require(set(item)=={"role","content","timestamp","platform_message_id"} and item["role"] in {"user","assistant"} and isinstance(item["content"],str) and item["platform_message_id"],"nonvisible_group_history_rejected")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".squire-group-import-",dir=output.parent) as temporary:
        stage=Path(temporary);stage.chmod(0o700)
        session_id="squire-group-import-"+hashlib.sha256(conversation_id.encode()).hexdigest()[:24]
        key="agent:main:squire:group:"+conversation_id
        origin={"platform":"squire","chat_id":conversation_id,"chat_type":"group","user_id":"group-history","thread_id":None}
        state=SessionDB(stage/"state.db")
        try:
            state.create_session(session_id,"squire",session_key=key,chat_id=conversation_id,chat_type="group",origin_json=json.dumps(origin),transport_profile="default")
            for item in payload["messages"]:state.append_message(session_id,**item)
        finally:state.close()
        stamp=datetime.now(timezone.utc).isoformat()
        entry={"session_key":key,"session_id":session_id,"created_at":stamp,"updated_at":stamp,"platform":"squire","chat_type":"group","metadata":{},"origin":origin,"suspended":False,"resume_pending":False,"active_turn_token":None,"active_turn_started_at":None,"transport_profile":"default"}
        with offline_sqlite(stage/"state.db") as db:
            db.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES(?,?,?,?)",(runtime_sessions_dir,key,json.dumps(entry),datetime.now(timezone.utc).timestamp()))
            require(db.execute("PRAGMA integrity_check").fetchone()[0]=="ok","group_import_integrity_failed")
        atomic_json(stage/"sessions/sessions.json",{key:entry})
        atomic_json(stage/"IMPORT_RESULT.json",{"format":"squire-isolated-group-history-v1","conversation_id":conversation_id,"approved_claim_id":approved_claim_id,"message_count":len(payload["messages"]),"contains_private_memory":False,"contains_credentials":False,"source_history_sha256":manifest["history_sha256"],"runtime_start_authorized":False})
        os.replace(stage,output)
    return {"message_count":len(payload["messages"]),"runtime_start_authorized":False}


TRANSPORT_PARTS={"whatsapp","whatsapp-session","telegram","wacli","baileys"}
DIRECT_TRANSPORT=re.compile(rb"WHATSAPP_|TELEGRAM_|api\.telegram\.org|web\.whatsapp\.com|@s\.whatsapp\.net|@g\.us|\bwacli\s+send|\bbaileys\b",re.I)


def private_relative(value):
    path=Path(value)
    require(not path.is_absolute() and value not in {"", "."} and ".." not in path.parts,"unsafe_promotion_path")
    require(not any(part.lower().lstrip(".") in TRANSPORT_PARTS for part in path.parts),"legacy_transport_state_quarantined")
    require(path.parts[0]!=".squire","operator_managed_state_not_promotable")
    return path


def prepare_private_environment(source,output,expected_checksum):
    source,output=Path(source),Path(output)
    require(checksum(source)==expected_checksum and not output.exists(),"environment_snapshot_changed_or_output_exists")
    retained=[];quarantined=[]
    exact={"INTERNAL_API_TOKEN","CONTROL_API_URL","HOME","HERMES_HOME","HERMES_MANAGED_DIR","HERMES_SAFE_MODE","PYTEST_CURRENT_TEST","PORT"}
    prefixes=("WHATSAPP_","TELEGRAM_","DISCORD_","SLACK_","SIGNAL_","SQUIRE_","MESSAGING_","WACLI_")
    for line in source.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):continue
        match=re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$",line.strip())
        require(match is not None,"multiline_or_unrecognized_environment_requires_review")
        name,value=match.groups()
        if name in exact or name.startswith(prefixes):quarantined.append(name);continue
        if value.startswith(("'",'"')):require(len(value)>=2 and value[-1]==value[0],"multiline_environment_requires_review")
        retained.append(line)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text("\n".join(retained)+"\n");output.chmod(0o600)
    return {"retained_variables":len(retained),"quarantined_variable_names":quarantined,"source_sha256":expected_checksum,"target_sha256":checksum(output)}


def prepare_runtime_config(source,output,expected_checksum,conversation_id,approved_mcp_servers=(),skill_paths=()):
    import yaml
    source,output=Path(source),Path(output)
    require(checksum(source)==expected_checksum and not output.exists() and SAFE_PART.fullmatch(conversation_id),"runtime_config_review_required")
    config=yaml.safe_load(source.read_text());require(isinstance(config,dict),"invalid_source_runtime_config")
    servers=config.get("mcp_servers") or {}
    require(set(approved_mcp_servers)<=set(servers),"unknown_approved_mcp_server")
    config["mcp_servers"]={key:servers[key] for key in approved_mcp_servers}
    require(not DIRECT_TRANSPORT.search(json.dumps(config["mcp_servers"]).encode()),"direct_send_mcp_requires_reauthoring")
    require(all(Path(path).is_absolute() and str(path).startswith("/opt/data/") for path in skill_paths),"runtime_skill_path_required")
    tools=(config.get("platform_toolsets") or {}).get("whatsapp")
    config["platform_toolsets"]={"squire":tools} if tools else {}
    config["platforms"]={"squire":{"enabled":True,"home_channel":{"platform":"squire","chat_id":conversation_id},"gateway_restart_notification":False,"typing_indicator":False}}
    config["plugins"]={"enabled":["squire-overlay"],"disabled":[]};config["hooks"]={}
    for legacy in ("whatsapp","telegram","discord","slack","signal","webhook"):config.pop(legacy,None)
    config.setdefault("skills",{})["external_dirs"]=list(skill_paths)
    config.setdefault("memory",{})["provider"]="hindsight"
    config["group_sessions_per_user"]=False
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(yaml.safe_dump(config,sort_keys=False));output.chmod(0o600)
    return {"platforms":["squire"],"approved_mcp_servers":list(approved_mcp_servers),"source_sha256":expected_checksum,"target_sha256":checksum(output)}


def promote_private_files(source,prepared,output,plan):
    """Positive file allowlist; source snapshot and unknown scripts stay quarantined."""
    source,prepared,output=Path(source),Path(prepared),Path(output)
    require(plan.get("kind")=="dm" and SAFE_PART.fullmatch(plan.get("runtime_id","")) and plan.get("migration_id"),"private_runtime_promotion_required")
    require(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}",plan.get("image_ref","")) and not output.exists(),"immutable_image_and_new_output_required")
    approved=[];destinations=set();credentials=set()
    modes={}
    if plan.get("source_modes"):
        descriptor=plan["source_modes"]
        mode_root=source if descriptor.get("root")=="snapshot" else prepared if descriptor.get("root")=="prepared" else None
        require(mode_root is not None,"source_mode_root_required")
        mode_path=mode_root/private_relative(descriptor["path"])
        require(mode_path.is_file() and not mode_path.is_symlink() and mode_path.resolve().is_relative_to(mode_root.resolve()) and checksum(mode_path)==descriptor["sha256"],"source_mode_metadata_changed")
        modes=json.loads(mode_path.read_text())["files"]
    for item in plan.get("files",[]):
        require(item.get("reviewed_no_direct_transport") is True,"file_requires_explicit_review")
        relative=private_relative(item["source"]);destination=private_relative(item["destination"])
        require(str(destination) not in destinations,"duplicate_promotion_destination");destinations.add(str(destination))
        root=source if item.get("root")=="snapshot" else prepared if item.get("root")=="prepared" else None
        require(root is not None,"promotion_root_required")
        logical=relative
        if item.get("snapshot_section") is not None:
            require(root==source and item["snapshot_section"] in {"core","integrations"} and relative.parts[0]==item["snapshot_section"],"snapshot_section_mismatch")
            logical=Path(*relative.parts[1:])
        expected_destination=str(logical).removeprefix(".hermes/") if root==source else str(relative)
        require(str(destination)==expected_destination,"source_layout_mapping_mismatch")
        path=root/relative
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()) and checksum(path)==item["sha256"],"promoted_file_changed")
        category=item.get("category")
        require(category in {"memory","credential","browser","workspace","skill","hindsight_plugin","runtime_config","environment"},"unreviewed_promotion_category")
        if str(destination) in {"config.yaml",".env","hindsight/config.json"}:require(root==prepared,"config_requires_prepared_scoped_transform")
        if str(destination).startswith("plugins/"):require(str(destination).startswith("plugins/hindsight/"),"legacy_plugin_quarantined")
        if category in {"workspace","skill","hindsight_plugin","runtime_config","environment"}:
            require(not DIRECT_TRANSPORT.search(path.read_bytes()),"direct_transport_code_quarantined")
        require(category!="browser" and destination.parts[0]!=".camofox","browser_requires_encrypted_state_import")
        if category in {"credential","environment"}:
            require(all(re.fullmatch(r"[A-Za-z0-9_.-]+",part) and part not in {".",".."} for part in destination.parts),"unsafe_sealed_file_name")
            credentials.add(str(destination))
        executable=False
        if item.get("source_mode") is not None:
            mode=item["source_mode"];record=modes.get(item.get("source_mode_path",str(logical)))
            require(isinstance(mode,int) and not isinstance(mode,bool) and 0<=mode<=0o777 and isinstance(record,dict) and record.get("source_mode")==mode and record.get("sha256")==item["sha256"] and record.get("size")==path.stat().st_size,"source_mode_review_mismatch")
            executable=bool(mode&0o111)
            require(not executable or category in {"workspace","skill"},"executable_category_not_allowed")
        approved.append((path,destination,category,executable,item["sha256"]))
    mandatory={"config.yaml","SOUL.md","hindsight/config.json","plugins/hindsight/__init__.py"}
    require(mandatory<=destinations,"restored_home_mandatory_files_missing")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".squire-private-promotion-",dir=output.parent) as temporary:
        stage=Path(temporary);stage.chmod(0o700)
        for path,destination,category,executable,expected_sha in approved:
            target=stage/destination;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target);target.chmod(0o700 if executable else 0o600)
            require(checksum(target)==expected_sha and checksum(path)==expected_sha,"source_changed_during_promotion")
        files={str(path.relative_to(stage)):checksum(path) for path in stage.rglob("*") if path.is_file()}
        # Marker is the LAST write: entrypoint refuses unreviewed restored homes.
        atomic_json(stage/".squire/restored-home.json",{"format":"squire-restored-home-v1","runtime_id":plan["runtime_id"],"migration_id":plan["migration_id"],"image_ref":plan["image_ref"],"files":files,"credential_protection":{"existing_envelope":sorted(credentials&{".env","auth.json"}),"owner_only_mode0600":sorted(credentials-{".env","auth.json"})}})
        os.replace(stage,output)
    return {"promoted_files":len(approved),"unselected_source_files_quarantined":True,"runtime_start_authorized":False,"layout":"HOME=HERMES_HOME=/opt/data","credential_protection":{"existing_envelope":sorted(credentials&{".env","auth.json"}),"owner_only_mode0600":sorted(credentials-{".env","auth.json"})}}


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest="command",required=True)
    inv=sub.add_parser("cron-inventory");inv.add_argument("source",type=Path);inv.add_argument("output",type=Path)
    transform=sub.add_parser("cron-transform");transform.add_argument("source",type=Path);transform.add_argument("output",type=Path);transform.add_argument("plan",type=Path)
    memory=sub.add_parser("memory-config");memory.add_argument("source",type=Path);memory.add_argument("output",type=Path);memory.add_argument("--expected-sha256",required=True)
    group=sub.add_parser("group-history");group.add_argument("source",type=Path);group.add_argument("output",type=Path);group.add_argument("--conversation-id",required=True);group.add_argument("--approved-claim-id",required=True);group.add_argument("--runtime-sessions-dir",required=True)
    env=sub.add_parser("private-environment");env.add_argument("source",type=Path);env.add_argument("output",type=Path);env.add_argument("--expected-sha256",required=True)
    cfg=sub.add_parser("runtime-config");cfg.add_argument("source",type=Path);cfg.add_argument("output",type=Path);cfg.add_argument("--expected-sha256",required=True);cfg.add_argument("--conversation-id",required=True);cfg.add_argument("--approved-mcp",action="append",default=[]);cfg.add_argument("--skill-path",action="append",default=[])
    promotion=sub.add_parser("promote-private");promotion.add_argument("source",type=Path);promotion.add_argument("prepared",type=Path);promotion.add_argument("output",type=Path);promotion.add_argument("plan",type=Path)
    args=parser.parse_args()
    if args.command=="promote-private":print(json.dumps(promote_private_files(args.source,args.prepared,args.output,json.loads(args.plan.read_text()))))
    elif args.command=="private-environment":print(json.dumps(prepare_private_environment(args.source,args.output,args.expected_sha256)))
    elif args.command=="runtime-config":print(json.dumps(prepare_runtime_config(args.source,args.output,args.expected_sha256,args.conversation_id,args.approved_mcp,args.skill_path)))
    elif args.command=="memory-config":print(json.dumps(prepare_memory_config(args.source,args.output,args.expected_sha256)))
    elif args.command=="group-history":print(json.dumps(import_group_history(args.source,args.output,args.conversation_id,args.approved_claim_id,args.runtime_sessions_dir)))
    elif args.command=="cron-inventory":
        require(not args.output.exists(),"inventory_output_exists");atomic_json(args.output,cron_inventory(args.source));print(json.dumps({"inventory_written":True}))
    else:print(json.dumps(transform_cron(args.source,args.output,json.loads(args.plan.read_text()))))

if __name__=="__main__":main()
