"""Allowlisted callers use argv; subprocess output is never logged."""
import asyncio
import json
import os
import signal


class CLIError(Exception):
    def __init__(self, code: str, *, definitely_unsent: bool = False):
        super().__init__(code)
        self.code = code
        self.definitely_unsent = definitely_unsent


class Wacli:
    def __init__(self, settings):
        self.settings = settings

    def argv(self, *args, readonly=False):
        result = [self.settings.wacli_binary, "--store", str(self.settings.store_dir),
                  "--json", "--timeout", f"{self.settings.command_timeout}s"]
        if readonly:
            result.append("--read-only")
        return [*result, *args]

    async def run(self, *args, readonly=False):
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(*args, readonly=readonly), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise CLIError("cli_unavailable", definitely_unsent=True) from None

        async def collect():
            chunks, size = [], 0
            while chunk := await proc.stdout.read(65536):
                size += len(chunk)
                if size > self.settings.max_output_bytes:
                    raise CLIError("cli_output_limit")
                chunks.append(chunk)
            await proc.wait()
            return b"".join(chunks)

        try:
            raw = await asyncio.wait_for(collect(), timeout=self.settings.command_timeout + 5)
        except (TimeoutError, asyncio.CancelledError, CLIError):
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            raise CLIError("cli_outcome_unknown") from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CLIError("cli_invalid_response") from None
        if proc.returncode or not result.get("success"):
            # Only the upstream explicit pre-dispatch refusal proves no send.
            error = str(result.get("error", ""))
            if "it was not sent" in error:
                raise CLIError("queue_deadline", definitely_unsent=True)
            if "checkpoint" in error.lower() and any(s in error.lower() for s in ("expir", "gap", "invalid", "different")):
                raise CLIError("checkpoint_invalid")
            raise CLIError("cli_failed")
        return result.get("data")
