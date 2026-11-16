import asyncio
import contextlib
import fcntl
import json
import os
import signal


class Supervisor:
    """Own one sync process. CLI delegation sends through this process's IPC socket."""
    def __init__(self, settings, cli, ledger):
        self.settings, self.cli, self.ledger = settings, cli, ledger
        self.lock = asyncio.Lock()
        self.process = None
        self.task = None
        self.connected = False
        self.stopping = False
        self.paused = False
        self.owner = None

    def acquire(self):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.settings.require_mount and not os.path.ismount(self.settings.data_dir):
            raise RuntimeError("persistent_volume_required")
        self.owner = open(self.settings.data_dir / "transport.lock", "a")
        fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def start(self):
        self.acquire()
        if self.settings.sync_enabled:
            self.task = asyncio.create_task(self.follow())

    async def follow(self):
        delay = 1
        while not self.stopping:
            if self.paused or self.ledger.get("logged_out", False):
                await asyncio.sleep(1)
                continue
            try:
                async with self.lock:
                    if self.paused or self.stopping:
                        continue
                    self.process = await asyncio.create_subprocess_exec(
                        *self.cli.argv("--events", "sync", "--follow", "--max-reconnect", "0",
                                       "--max-db-size", self.settings.max_db_size,
                                       "--send-spacing", f"{self.settings.send_spacing_seconds}s"),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                        start_new_session=True, limit=1024 * 1024)
                    proc = self.process
                while line := await proc.stderr.readline():
                    try:
                        event = json.loads(line).get("event")
                    except (ValueError, AttributeError):
                        continue
                    if event == "connected":
                        self.connected, delay = True, 1
                    elif event in {"disconnected", "logged_out"}:
                        self.connected = False
                        if event == "logged_out":
                            self.ledger.put("logged_out", True)
                            self.ledger.put("outbound_enabled", False)
                await proc.wait()
            except (OSError, ValueError):
                self.ledger.put("sync_error", "process_failed")
            finally:
                self.connected = False
                await self.stop_process()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def stop_process(self):
        proc = self.process
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        self.connected = False

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """Live metadata commands are not delegated upstream: drain/stop first."""
        async with self.lock:
            self.paused = True
            await self.stop_process()
            try:
                yield
            finally:
                self.paused = False

    async def stop(self):
        self.stopping = True
        await self.stop_process()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.owner:
            self.owner.close()
