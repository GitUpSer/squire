"""Squire overlay registered through Hermes' supported module entry point."""
import hashlib
import json
import os
from .config import RuntimeConfig
from .store import Store

__version__="0.4.0"
GROUP_TOOLS=frozenset({"memory","squire_request_private_action"})


def register(ctx):
    if not os.environ.get("SQUIRE_RUNTIME_ID"):return  # Legacy Telegram stays on its existing path.
    scope=RuntimeConfig.from_env();store=Store(scope.database)
    from .adapter import SquireAdapter,standalone_send
    registration=ctx.register_platform("squire","Squire",SquireAdapter,lambda:True,
        validate_config=lambda _:bool(os.environ.get("SQUIRE_RUNTIME_ID")),
        is_connected=lambda _:bool(os.environ.get("SQUIRE_RUNTIME_ID")),
        required_env=["SQUIRE_RUNTIME_ID"],allow_all_env="SQUIRE_SCOPED_ALLOW_ALL",
        env_enablement_fn=lambda:{"gateway_restart_notification":False,"typing_indicator":False},
        pii_safe=True,allow_update_command=False,max_message_length=32000,
        cron_deliver_env_var="SQUIRE_HOME_CHANNEL",standalone_sender_fn=standalone_send,
        validate_target_ref_fn=lambda target:target==scope.conversation_id)
    if registration is None:raise RuntimeError("Squire platform registration rejected")

    def pre_tool(tool_name,args=None,tool_call_id="",**kwargs):
        if scope.kind=="group" and tool_name not in GROUP_TOOLS:
            return {"action":"block","message":"This group agent cannot access private accounts, files, browsers, shells or external destinations. Request an owner-approved private action instead."}
        if store.tool_start(tool_name,args or {},tool_call_id) is False:
            return {"action":"block","message":"This tool operation was already attempted. Its outcome must be reconciled before retrying."}
    def post_tool(tool_name,args=None,tool_call_id="",**kwargs):
        store.tool_finish(tool_name,args or {},tool_call_id)
    ctx.register_hook("pre_tool_call",pre_tool)
    ctx.register_hook("post_tool_call",post_tool)

    def request_private_action(args,**kwargs):
        from .client import Broker,BrokerError
        if scope.kind!="group":return json.dumps({"error":"only approved groups can request private actions"})
        summary=str(args.get("summary","")).strip()
        if not summary or len(summary)>2000:return json.dumps({"error":"provide a bounded action description"})
        active=store.active_event()
        if not active:return json.dumps({"error":"an active group request is required"})
        key=hashlib.sha256((active+":"+summary).encode()).hexdigest()
        try:return json.dumps(Broker(scope).request("POST","/v1/approvals",{"request_key":key,"summary":summary}))
        except BrokerError:return json.dumps({"error":"approval service unavailable; no action has been authorized"})
    ctx.register_tool(name="squire_request_private_action",toolset="squire-group",
        schema={"name":"squire_request_private_action","description":"Ask the sponsoring owner to approve a specific private action. Approval does not release its result to this group; the owner must separately choose what to share.","parameters":{"type":"object","properties":{"summary":{"type":"string","maxLength":2000}},"required":["summary"],"additionalProperties":False}},
        handler=request_private_action,check_fn=lambda:scope.kind=="group")
