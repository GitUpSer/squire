"""Execute behavior extracted from real pinned source, without Hermes' ML stack.

Pass pristine upstream source (copies files and applies real patches), or pass
--applied inside the image. Test source is never reconstructed from patch context.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
from contextlib import suppress
from unittest.mock import AsyncMock, patch

PATCHES=Path(__file__).resolve().parent
FILES=("gateway/run.py","gateway/run_turn.py","gateway/run_shutdown.py",
       "plugins/platforms/telegram/adapter.py","agent/anthropic_adapter.py","utils.py")


def tree(root,file):return ast.parse((root/file).read_text())


def function(root,file,name,namespace):
    node=next(n for n in ast.walk(tree(root,file)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    module=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),node],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),str(root/file),"exec"),namespace)
    return namespace[name]


def status_gate(root):
    fn=function(root,"gateway/run.py","_prepare_gateway_status_message",{
        "os":os,"_gateway_surface_passes_raw_text":lambda p:p=="local",
        "_redact_gateway_user_facing_secrets":lambda t:t,
        "_TELEGRAM_NOISY_STATUS_RE":re.compile(r"^routine compression$"),
        "_gateway_compression_progress_notices_enabled":lambda:False,
        "_COMPRESSION_PROGRESS_STATUS_RE":re.compile(r"^routine compression$"),
        "_looks_like_gateway_provider_error":lambda t:t=="provider error",
        "_gateway_provider_error_reply":lambda t:"A safe provider error"})
    for value in ("","0","off"," OFF "):
        with patch.dict(os.environ,{"SQUIRE_STATUS_NOTICES":value}):
            assert fn("squire","lifecycle","operator detail") is None
            assert fn("squire","warn","provider error")=="A safe provider error"
            assert fn("local","lifecycle","operator detail")=="operator detail"
    with patch.dict(os.environ,{"SQUIRE_STATUS_NOTICES":"all"}):
        assert fn("squire","lifecycle","operator detail")=="operator detail"


def shutdown_gate(root):
    fn=function(root,"gateway/run_shutdown.py","_notify_active_sessions_of_shutdown",{"os":os,"logger":logging.getLogger("test")})
    for value in ("","0"," 0 "):
        with patch.dict(os.environ,{"SQUIRE_SHUTDOWN_NOTICES":value}):
            asyncio.run(fn(object()))
    with patch.dict(os.environ,{"SQUIRE_SHUTDOWN_NOTICES":"all"}):
        try:asyncio.run(fn(object()))
        except AttributeError:pass
        else:raise AssertionError("shutdown opt-in swallowed upstream branch")


def concierge_gate(root):
    class Platform:
        LOCAL="local"
        WEBHOOK="webhook"
        SLACK="slack"
    class P:
        value="squire"
    platform=P();saved=[]
    fn=function(root,"gateway/run_turn.py","_hmwa_first_contact_notes",{
        "os":os,"json":json,"suppress":suppress,"Platform":Platform,"logger":logging.getLogger("test")})
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);(home/".squire").mkdir();state=home/".squire/concierge-state.json"
        cfg=types.SimpleNamespace(platforms={platform:types.SimpleNamespace(home_channel=None)},get_home_channel=lambda _:None)
        source=types.SimpleNamespace(platform=platform,chat_id="opaque-conversation",profile="default")
        runner=types.SimpleNamespace(async_session_store=types.SimpleNamespace(has_any_sessions=AsyncMock(return_value=False)),
            config=cfg,_deliver_platform_notice=AsyncMock())
        mods={"gateway.run":types.SimpleNamespace(_hermes_home=home,_home_target_env_var=lambda _:None,_load_gateway_config=lambda:{}),
            "agent.onboarding":types.SimpleNamespace(PROFILE_BUILD_FLAG="test",is_seen=lambda *a:False,
                mark_seen=lambda *a:None,profile_build_directive=lambda:"competing prompt",profile_build_mode=lambda _:"off"),
            "gateway.config":types.SimpleNamespace(HomeChannel=lambda **kw:types.SimpleNamespace(**kw),persist_home_channel=saved.append)}
        with patch.dict(sys.modules,mods):
            state.write_text(json.dumps({"state":"greet"}))
            notes=[];asyncio.run(fn(runner,source,[],notes))
            assert notes==[] and saved[0].chat_id=="opaque-conversation"
            runner._deliver_platform_notice.assert_not_awaited()
            state.write_text(json.dumps({"state":"complete"}))
            notes=[];asyncio.run(fn(runner,source,[],notes))
            assert len(notes)==1 and "first message" in notes[0]
            state.write_text("invalid json")
            notes=[];asyncio.run(fn(runner,source,[],notes));assert len(notes)==1
            notes=[];asyncio.run(fn(runner,source,[{"role":"user"}],notes));assert notes==[]


def telegram_gate(root):
    source=tree(root,"plugins/platforms/telegram/adapter.py")
    connector=next(n for n in ast.walk(source) if isinstance(n,ast.AsyncFunctionDef) and n.name=="connect")
    guard=next(n for n in ast.walk(connector) if isinstance(n,ast.If)
        and ast.unparse(n.test)=="self._webhook_mode" and "_bot_identity_refresh_loop" in ast.unparse(n))
    for ttl in (-1,0,300):
        calls=[]
        obj=types.SimpleNamespace(_webhook_mode=True,_BOT_IDENTITY_TTL_SECONDS=ttl,name="test",
            _restart_task_attr=lambda *args:calls.append(args),_bot_identity_refresh_loop=lambda:"loop")
        exec(compile(ast.fix_missing_locations(ast.Module(body=[guard],type_ignores=[])),"connect-block","exec"),
             {"self":obj,"logger":logging.getLogger("test")})
        assert len(calls)==(1 if ttl>0 else 0)
    assignment=next(n for n in ast.walk(source) if isinstance(n,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=="_BOT_IDENTITY_TTL_SECONDS" for t in n.targets))
    assert ast.unparse(assignment.value)=="env_float('SQUIRE_TELEGRAM_IDENTITY_TTL', 300.0)"


def upstream_oauth_gate(root):
    names={"_CLAUDE_CODE_SYSTEM_PREFIX","_MCP_TOOL_PREFIX","_OAUTH_TOOL_NAME_ALIASES", "_OAUTH_TOOL_NAME_REVERSE_ALIASES",
           "_OAUTH_PROSE_ALIAS_NAMES","_OAUTH_PROSE_ALIAS_PATTERNS","_OAUTH_SYSTEM_REPLACEMENTS","_OAUTH_SLUG_PATTERN"}
    functions={"_apply_oauth_prose_aliases","_normalize_to_mcp_wire","_oauth_wire_namer","_apply_claude_code_identity"}
    body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0)]
    for node in tree(root,"agent/anthropic_adapter.py").body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets):body.append(node)
        elif isinstance(node,ast.FunctionDef) and node.name in functions:body.append(node)
    ns={"re":re};exec(compile(ast.fix_missing_locations(ast.Module(body=body,type_ignores=[])),"actual-oauth-functions","exec"),ns)
    tools=[{"name":"session_search"},{"name":"memory"},{"name":"mcp_calendar_read"}]
    messages=[{"content":[{"type":"tool_use","name":"session_search"}]}]
    system=ns['_apply_claude_code_identity']("Use session_search from /home/hermes/.hermes/hermes-agent",tools,messages,ns['_oauth_wire_namer'](tools))
    assert [t['name'] for t in tools]==['mcp__chat_history_lookup','mcp__context_notes','mcp__calendar_read']
    assert messages[0]['content'][0]['name']=='mcp__chat_history_lookup'
    assert '/home/hermes/.hermes/hermes-agent' in system[-1]['text']
    assert ns['_oauth_wire_namer']([{'name':'session_search'},{'name':'chat_history_lookup'}])('session_search')=='mcp__session_search'


def check(root):
    subprocess.run(["bash",str(PATCHES/"verify-markers.sh"),str(root)],check=True)
    for file in FILES:compile((root/file).read_text(),str(root/file),"exec")
    for fn in (status_gate,shutdown_gate,concierge_gate,telegram_gate,upstream_oauth_gate):
        fn(root);print("PASS actual pinned-source "+fn.__name__)


def main():
    parser=argparse.ArgumentParser();parser.add_argument("source",type=Path);parser.add_argument("--applied",action="store_true");args=parser.parse_args()
    if args.applied:check(args.source)
    else:
        with tempfile.TemporaryDirectory(prefix="squire-patch-compatibility-") as td:
            root=Path(td)
            for file in FILES:
                (root/file).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(args.source/file,root/file)
            subprocess.run(["bash",str(PATCHES/"apply-patches.sh"),str(root)],check=True);check(root)


if __name__=="__main__":main()
