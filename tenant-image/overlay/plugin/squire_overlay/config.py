"""Immutable per-process scope. No account transport credentials belong here."""
from dataclasses import dataclass
import os
from pathlib import Path

@dataclass(frozen=True)
class RuntimeConfig:
    runtime_id: str
    tenant_id: str
    conversation_id: str
    kind: str
    ingress_token: str
    egress_token: str
    control_token: str
    messaging_url: str
    control_url: str
    generation: int
    home: Path

    @classmethod
    def from_env(cls):
        required=("SQUIRE_RUNTIME_ID","SQUIRE_OWNER_TENANT_ID","SQUIRE_CONVERSATION_ID","SQUIRE_RUNTIME_KIND","SQUIRE_INGRESS_TOKEN","SQUIRE_EGRESS_TOKEN","SQUIRE_CONTROL_TOKEN","SQUIRE_MESSAGING_URL","CONTROL_API_URL")
        if any(not os.environ.get(key) for key in required):
            raise ValueError("incomplete Squire runtime configuration")
        if os.environ["SQUIRE_RUNTIME_KIND"] not in {"dm","group"}: raise ValueError("invalid runtime kind")
        tokens=[os.environ[key] for key in ("SQUIRE_INGRESS_TOKEN","SQUIRE_EGRESS_TOKEN","SQUIRE_CONTROL_TOKEN")]
        if any(len(token)<32 for token in tokens) or len(set(tokens))!=3: raise ValueError("invalid scoped capabilities")
        return cls(os.environ[required[0]],os.environ[required[1]],os.environ[required[2]],os.environ[required[3]],*tokens,os.environ["SQUIRE_MESSAGING_URL"].rstrip("/"),os.environ["CONTROL_API_URL"].rstrip("/"),int(os.environ.get("SQUIRE_DEPLOYMENT_GENERATION","0")),Path(os.environ.get("HERMES_HOME","/opt/data")))

    @property
    def database(self): return self.home/".squire"/"messaging.sqlite3"
