import asyncio
import contextlib
import hmac
import logging
import os
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .broker import Broker, Send
from .capture import Capture
from .cli import CLIError, Wacli
from .config import Settings
from .ledger import Conflict, Ledger
from .supervisor import Supervisor
from .retention import Retention
from .media import Media, FetchMedia
from .responses import EphemeralFileResponse


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1)
    outbound_enabled: bool


class RetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(default=7, ge=1, le=30)
    limit: int = Field(default=100, ge=1, le=100)
    dry_run: bool = True


def create_app(settings=None, *, cli=None, client=None):
    settings = settings or Settings()
    cli = cli or Wacli(settings)
    ledger = Ledger(settings.data_dir / "transport.sqlite3")
    supervisor = Supervisor(settings, cli, ledger)
    capture = Capture(settings, cli, ledger, client)
    broker = Broker(settings, cli, ledger, supervisor, capture)
    retention = Retention(settings, cli, ledger, supervisor, capture)
    media = Media(settings, cli)

    @asynccontextmanager
    async def lifespan(app):
        if settings.pairing_mode:
            # SSH remains available, but the private interactive pair CLI is the
            # only owner of the account store/lock in this boot mode.
            try:
                yield
            finally:
                await capture.client.aclose()
            return
        await supervisor.start()
        try:
            ledger.initialize(settings.generation, settings.outbound_enabled)
            task = asyncio.create_task(capture.follow())
            retention_task = asyncio.create_task(retention.follow())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task
                await capture.client.aclose()
        finally:
            await supervisor.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broker, app.state.capture = broker, capture
    app.state.ledger, app.state.supervisor = ledger, supervisor

    async def principal(token, expected):
        if not token or not hmac.compare_digest(token, "Bearer " + expected):
            raise HTTPException(401, "unauthorized")

    async def messaging(authorization: str | None = Header(default=None)):
        await principal(authorization, settings.broker_token)
        if settings.pairing_mode:
            raise HTTPException(503, "pairing_mode")

    async def operator(authorization: str | None = Header(default=None)):
        await principal(authorization, settings.operator_token)

    async def operator_mutation(authorization: str | None = Header(default=None)):
        await operator(authorization)
        if settings.pairing_mode:
            raise HTTPException(503, "pairing_mode")

    async def backup_principal(authorization: str | None = Header(default=None)):
        if not settings.backup_token:
            raise HTTPException(503, "backup_unconfigured")
        await principal(authorization, settings.backup_token)
        if settings.pairing_mode:
            raise HTTPException(503, "pairing_mode")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default includes submitted text and credentials.
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @app.exception_handler(Conflict)
    async def conflict_error(request, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(CLIError)
    async def cli_error(request, exc):
        return JSONResponse(status_code=503, content={"error": exc.code})

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        limit = 150000
        if request.url.path == "/v1/send":
            if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + settings.broker_token):
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
            limit = 14150000
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid_length"})
        if declared > limit:
            return JSONResponse(status_code=413, content={"error": "body_too_large"})
        if request.method in {"POST", "PUT", "PATCH"}:
            size = 0
            parts = []
            async for part in request.stream():
                size += len(part)
                if size > limit:
                    return JSONResponse(status_code=413, content={"error": "body_too_large"})
                parts.append(part)
            request._body = b"".join(parts)
        return await call_next(request)

    @app.get("/healthz")
    async def health():
        return {"alive": True}

    @app.get("/readyz")
    async def ready():
        ready = supervisor.connected and ledger.get("capture_enabled", False) and not capture.error
        return JSONResponse(status_code=200 if ready else 503, content={"ready": bool(ready)})

    @app.get("/v1/status", dependencies=[Depends(operator)])
    async def status():
        return dict(provider_account_id=settings.provider_account_id, generation=ledger.get("generation"),
            pairing_mode=settings.pairing_mode,
            connected=supervisor.connected, logged_out=ledger.get("logged_out", False),
            outbound_enabled=ledger.get("outbound_enabled", False), capture_enabled=ledger.get("capture_enabled", False),
            capture_error=capture.error, last_commit=capture.last_commit,
            retention=ledger.get("retention_status"),
            capabilities={"native_mentions": False, "reply_to": True, "group_info": True,
                          "media_download": True, "media_send": True, "media_max_bytes": 10485760}, wacli_version="0.26.0")

    @app.post("/v1/send", dependencies=[Depends(messaging)])
    async def send(request: Send):
        return await broker.send(request)

    @app.post("/v1/media/fetch", dependencies=[Depends(messaging)])
    async def fetch_media(request: FetchMedia):
        root, target, metadata = await media.fetch(request)
        return EphemeralFileResponse(target, private_root=root, media_type=metadata["mime_type"], filename=metadata["filename"],
                            headers={"X-Media-Type": metadata["type"], "Cache-Control": "no-store",
                                     "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/groups/{group_jid}", dependencies=[Depends(messaging)])
    async def group(group_jid: str):
        return await broker.group(group_jid)

    @app.post("/v1/groups/{group_jid}/leave", dependencies=[Depends(operator_mutation)])
    async def leave(group_jid: str):
        await broker.leave(group_jid)
        return {"left": True}

    @app.post("/v1/operator/control", dependencies=[Depends(operator_mutation)])
    async def control(request: Control):
        async with supervisor.lock:
            ledger.control(generation=request.generation, outbound_enabled=request.outbound_enabled)
        return {"generation": request.generation, "outbound_enabled": request.outbound_enabled}

    @app.post("/v1/operator/activate", dependencies=[Depends(operator_mutation)])
    async def activate():
        if not supervisor.connected:
            raise HTTPException(409, "pair_and_connect_first")
        try:
            await capture.activate()
        except RuntimeError:
            raise HTTPException(409, "activation_conflict") from None
        return {"activated": True}

    @app.post("/v1/operator/retention", dependencies=[Depends(operator_mutation)])
    async def prune(request: RetentionRequest):
        return await retention.run(**request.model_dump())

    @app.post("/v1/operator/backup", dependencies=[Depends(backup_principal)])
    async def backup():
        from .backup import snapshot_transport
        try:
            root, archive = await snapshot_transport(settings, supervisor, capture)
        except Exception:
            raise HTTPException(503, "backup_failed") from None
        return EphemeralFileResponse(archive, private_root=root, media_type="application/octet-stream", filename="transport.age")

    return app


def main():
    import uvicorn
    # Transport credentials, phone destinations and payloads must not enter request logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        app = create_app()
    except ValidationError:
        raise SystemExit("invalid transport configuration") from None
    host, port = os.environ.get("TRANSPORT_HOST", "::"), int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host=host, port=port, access_log=False)
    if host == "::":
        # asyncio's IPv6 listener is V6ONLY; Railway health probes use IPv4,
        # whereas its private service DNS resolves IPv6. Bind both explicitly.
        with socket.create_server((host, port), family=socket.AF_INET6, dualstack_ipv6=True) as listener:
            uvicorn.Server(config).run(sockets=[listener])
    else:
        uvicorn.Server(config).run()
