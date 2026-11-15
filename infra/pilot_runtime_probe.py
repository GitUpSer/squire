#!/usr/bin/env python3
"""Show executable/SQLite metadata, never full argv or environment."""
import json
import subprocess
from pathlib import Path

pid = int(subprocess.run(["systemctl", "--user", "show", "hermes-gateway.service", "--property=MainPID", "--value"],
                         capture_output=True, text=True, check=True).stdout.strip())
pending = [(pid, 0)]
seen = set()
while pending:
    current, depth = pending.pop(0)
    if current in seen or depth > 3:
        continue
    seen.add(current)
    proc = Path("/proc") / str(current)
    try:
        executable = (proc / "exe").resolve()
        args = (proc / "cmdline").read_bytes().split(b"\0")
        invoked = Path(args[0].decode())
        record = {"pid": current, "executable": str(executable), "invoked_program": str(invoked)}
        if "python" in executable.name:
            interpreter = invoked if invoked.is_absolute() and invoked.is_file() else executable
            result = subprocess.run([str(interpreter), "-c", "import sys,sqlite3,json; print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'compile_options':[r[0] for r in sqlite3.connect(':memory:').execute('pragma compile_options')]}))"],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                record["runtime"] = json.loads(result.stdout)
        print(json.dumps(record), flush=True)
        children = (proc / "task" / str(current) / "children").read_text().split()
        pending.extend((int(child), depth + 1) for child in children)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        continue
