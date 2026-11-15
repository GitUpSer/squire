#!/usr/bin/env python3
"""Compatibility is checked against actual pinned v2026.9.24 source."""
import os
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
source=Path(os.environ.get("HERMES_TEST_ROOT", ".upstream/hermes"))
if not source.exists():
    raise SystemExit("Set HERMES_TEST_ROOT to the pristine pinned Hermes v2026.9.24 source; synthetic old-version snippets are no longer used.")
subprocess.run([sys.executable,str(root/"patches/compatibility.py"),str(source)],check=True)
