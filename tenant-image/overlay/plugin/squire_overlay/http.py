"""Scoped stdlib shim endpoints. Body is acknowledged only after SQLite commit."""
import hmac
import json
from .config import RuntimeConfig
from .store import Store,ScopeError,QueueFull


def handle(handler,path):
    if path not in {"/events","/events/recovery"}:return False
    try:scope=RuntimeConfig.from_env()
    except ValueError:handler._respond(503,{"error":"runtime unavailable"});return True
    expected="Bearer "+scope.ingress_token
    if not hmac.compare_digest(handler.headers.get("Authorization", ""),expected):
        handler.close_connection=True;handler._respond(401,{"error":"unauthorized"});return True
    try:
        if handler.headers.get("Transfer-Encoding"):raise ScopeError("unsupported framing")
        length=int(handler.headers.get("Content-Length","0"))
        if not 0<length<=1024*1024:raise ScopeError("invalid body size")
        payload=json.loads(handler.rfile.read(length))
        store=Store(scope.database)
        if path=="/events/recovery":
            if not isinstance(payload,dict) or set(payload)!={"event_id","decision"} or payload["decision"]!="reconciled_skip":raise ScopeError("explicit reconciliation required")
            handler._respond(200,{"reconciled":store.resolve(payload["event_id"])});return True
        if not isinstance(payload,dict) or set(payload)!={"events"}:raise ScopeError("invalid envelope")
        ids=store.accept(payload["events"],scope)
        handler._respond(202,{"accepted":True,"event_ids":ids})
    except (ValueError,ScopeError,TypeError,KeyError):handler._respond(422,{"error":"invalid scoped event"})
    except QueueFull:handler._respond(429,{"error":"runtime inbox full"})
    except Exception:handler._respond(503,{"error":"durable storage unavailable"})
    return True
