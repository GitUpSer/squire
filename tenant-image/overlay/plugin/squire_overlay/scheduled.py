"""Durable cron-delivery identity; never authorizes re-execution of a job's tools."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json

_delivery=ContextVar("squire_scheduled_delivery",default=None)


@contextmanager
def delivery_scope(job, *, for_failure=False):
    from cron.executions import get_execution
    execution_id=str(job.get("execution_id") or "")
    record=get_execution(execution_id) if execution_id else None
    if not record or str(record["job_id"])!=str(job.get("id")):
        raise ValueError("scheduled delivery requires a persisted matching execution")
    # A scheduler retry can have another attempt ID for the same scheduled fire.
    # Explicit manual runs have no occurrence and intentionally get a new identity.
    occurrence=record.get("scheduled_instant") or execution_id
    identity=[str(job["id"]),occurrence,"failure" if for_failure else "result"]
    token=_delivery.set(hashlib.sha256(json.dumps(identity,separators=(",",":")).encode()).hexdigest())
    try:yield
    finally:_delivery.reset(token)


def response_key(content):
    identity=_delivery.get()
    if not identity:return None
    # The same immutable delivery can cross live/standalone and chunked lanes.
    # Content identity avoids generating another operation when a lane times out.
    chunk=hashlib.sha256(str(content).encode()).hexdigest()
    return f"scheduled:{identity}:{chunk}"
