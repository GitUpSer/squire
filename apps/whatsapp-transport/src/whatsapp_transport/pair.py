"""Explicit interactive pairing, only with broker stopped and attached terminal."""
import argparse
import fcntl
import getpass
import os
import subprocess
import sys
from pathlib import Path

from .ledger import Ledger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("TRANSPORT_DATA_DIR", "/data")))
    parser.add_argument("--binary", default=os.environ.get("TRANSPORT_WACLI_BINARY", "/usr/local/bin/wacli"))
    parser.add_argument("--phone", action="store_true", help="privately prompt for phone pairing instead of QR")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Pairing requires a private interactive terminal; never send QR codes to logs.")
    if os.geteuid() == 0:
        if args.data_dir != Path("/data") or args.data_dir.stat().st_uid != 10001:
            raise SystemExit("Run pairing as the transport account on its initialized /data volume.")
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(args.data_dir / "transport.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Stop the transport broker before pairing.") from None
        command = [args.binary, "--store", str(args.data_dir / "wacli"), "auth",
                   "--history-days", "1", "--history-max-per-chat", "10"]
        if args.phone:
            phone = getpass.getpass("WhatsApp phone number (international format): ")
            if not phone.lstrip("+").isdigit() or not 7 <= len(phone.lstrip("+")) <= 15:
                raise SystemExit("Invalid phone number.")
            command.extend(["--phone", phone])
        result = subprocess.run(command, check=False)
        if result.returncode == 0:
            ledger = Ledger(args.data_dir / "transport.sqlite3")
            ledger.put("logged_out", False)
            ledger.put("outbound_enabled", False)
        raise SystemExit(result.returncode)
