"""Prepare a scoped pilot runtime without overwriting the owner's durable identity."""
import json
import os
from pathlib import Path
import tempfile
import yaml
from .config import RuntimeConfig


def atomic(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix=".squire-")
    try:
        with os.fdopen(fd,"w") as handle:handle.write(text);handle.flush();os.fsync(handle.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def prepare():
    scope=RuntimeConfig.from_env();home=scope.home
    marker=home/".squire"/"runtime-profile.json"
    if marker.exists():
        old=json.loads(marker.read_text())
        if (old["kind"],old["runtime_id"],old["conversation_id"])!=(scope.kind,scope.runtime_id,scope.conversation_id):raise RuntimeError("runtime volume scope mismatch")
    elif scope.kind=="group":
        # Group volumes must be new or explicitly exported group-only state. Never
        # initialize a group on top of a founder's credential/browser home.
        forbidden=("auth.json",".claude",".codex",".ssh","browser_profiles","whatsapp-session","whatsapp")
        for item in forbidden:
            candidate=home/item
            if candidate.exists() and (candidate.is_dir() or candidate.stat().st_size>2):raise RuntimeError("private state is not permitted in a group runtime")
    path=home/"config.yaml"
    cfg=yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg=cfg if isinstance(cfg,dict) else {}
    # Disable built-in WhatsApp and all other transports in the isolated service.
    # Only Squire's broker owns a socket to the shared WhatsApp account.
    cfg["platforms"]={"squire":{"enabled":True,"gateway_restart_notification":False,"typing_indicator":False,"home_channel":{"platform":"squire","chat_id":scope.conversation_id}}}
    cfg["group_sessions_per_user"]=False
    cfg.setdefault("display",{}).setdefault("platforms",{})["squire"]={"streaming":False,"tool_progress":"off"}
    cfg.setdefault("plugins",{})["enabled"]=["squire-overlay"]
    cfg["plugins"]["disabled"]=[]
    cfg.setdefault("logging",{})["level"]="WARNING"
    from .onboarding import prepare as prepare_onboarding
    prepare_onboarding(home,atomic)
    # Pilot onboarding is supplied by the supported plugin hook. Remove the
    # legacy shell hook, whose provider-first sales flow is not the pilot.
    cfg["hooks"]={}
    if scope.kind=="group":
        cfg["platform_toolsets"]={"squire":["memory","squire-group"]}
        cfg["mcp_servers"]={};cfg["skills"]={"external_dirs":[],"disabled":["*"]}
        cfg["hooks"]={};cfg["fallback_providers"]=[]
        cfg["model"]="anthropic:claude-sonnet-5"
        cfg["onboarding"]={"profile_build":"off"}
        cfg["agent"]={"max_turns":30,"gateway_timeout":300}
        atomic(home/"SOUL.md","You are the Squire assistant for this approved group. Use only this group's context and memory. Never claim access to a member's private conversations, accounts, tools or files. For a private action, request explicit owner approval with squire_request_private_action. Approval does not release a result to this group.\n")
        atomic(home/".squire"/"concierge-state.json",'{"state":"complete"}\n')
    atomic(path,yaml.safe_dump(cfg,sort_keys=False))
    # Sanitize persisted transport vars before Hermes loads .env. Keep owner
    # model/tool credentials in private runtimes; groups receive only their key.
    envpath=home/".env"
    if envpath.exists():
        lines=[]
        for line in envpath.read_text().splitlines():
            key=line.split("=",1)[0].strip().removeprefix("export ")
            if key.startswith(("WHATSAPP_","TELEGRAM_","DISCORD_","SLACK_")) or key in {"INTERNAL_API_TOKEN","HERMES_MANAGED_DIR","HERMES_SAFE_MODE","PYTEST_CURRENT_TEST"}:continue
            if scope.kind=="group" and key not in {"ANTHROPIC_API_KEY","ANTHROPIC_BASE_URL","SQUIRE_TRIAL_EXPIRES_AT"}:continue
            lines.append(line)
        # Preserve existing tmpfs symlink: write through it rather than replacing.
        envpath.write_text("\n".join(lines)+"\n")
    atomic(marker,json.dumps({"version":1,"hermes_floor":"v2026.9.24","kind":scope.kind,"runtime_id":scope.runtime_id,"conversation_id":scope.conversation_id},sort_keys=True))

if __name__=="__main__":prepare()
