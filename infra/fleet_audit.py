#!/usr/bin/env python3
"""Read-only fleet audit over `railway ssh`. **Phase ③ step 0a** (docs/overlay-phase3-plan.md).

    export CONTROL_API_URL=https://control-api...   INTERNAL_API_TOKEN=...
    python infra/fleet_audit.py -p squire-staging -e production
    python infra/fleet_audit.py -p squire-staging -e production --tenant +1ctest7 --snapshot \
        --out /tmp/audit-1ctest7-pre.json

For every tenant in `GET /internal/fleet` it runs `infra/fleet_audit_probe.py` INSIDE the
tenant container (see that file for exactly what it may report), then prints one table
row per tenant. Nothing on a tenant is changed. It answers the questions the Phase ③
drill needs before touching anything:

  * which image each tenant really runs, and its `_config_version`;
  * whether a home channel exists and is the owner (cron delivery / adoption);
  * whether the old concierge shell hook is consented (why v0.3.0 needs a stub);
  * gateway health incl. the s6 gateway-default ("Bug 3") signature;
  * where the concierge is, and the valve;
  * **any Anthropic OAuth source** — the gate for dropping patch 001;
  * `HERMES_MANAGED_DIR` / `PYTEST_CURRENT_TEST` / `HERMES_SAFE_MODE`, which silently
    disable the managed layer;
  * which tenants have no home AND no concierge state (plugin adoption can't reach them).

It also reads each service's Railway variables, but keeps only whether a few allowlisted
NAMES are set. `railway variable list --json` returns raw values; they are discarded the
moment they are parsed and never printed or written.

Caveats:
  * `railway ssh` WAKES a sleeping tenant. Auditing the whole fleet costs each tenant
    one wake.
  * `railway ssh` flakes: a JSON blob starting `{"status":` means the request hit
    Railway's SSH gateway, not the container. That is retried (memory:
    railway-ssh-tenant-access), anything else is reported as a failure.
  * Stopped tenants have no container and are listed, not probed.

Exit codes: 0 = every running tenant audited; 1 = config/control-api error or at
least one tenant could not be audited.
"""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import sys
import time
import zlib
from pathlib import Path

from _control_api_client import ControlAPI, ControlAPIError, fail

PROBE_PATH = Path(__file__).with_name("fleet_audit_probe.py")
#: The upstream venv interpreter inside the tenant image (has PyYAML).
REMOTE_PYTHON = "/opt/hermes/.venv/bin/python"
MARKER = "SQUIRE_AUDIT "
DEFAULT_RETRIES = 4
DEFAULT_SSH_TIMEOUT = 120.0
RETRY_DELAY_SECONDS = 3.0

#: Railway service variables whose PRESENCE matters. Values are never kept.
RAILWAY_VAR_NAMES = (
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "HERMES_MANAGED_DIR",
    "PYTEST_CURRENT_TEST",
    "HERMES_SAFE_MODE",
    "SQUIRE_UNBOUND_AWAKE_HOURS",
)
#: Prefix-matched (alpha6 carries a placeholder cloud-embeddings key).
RAILWAY_VAR_PREFIXES = ("HINDSIGHT_API_EMBEDDINGS_",)


class AuditError(RuntimeError):
    pass


# --------------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------------
def encoded_probe() -> str:
    """The probe source, zlib-compressed and base64-encoded (shell-safe alphabet)."""
    return base64.b64encode(zlib.compress(PROBE_PATH.read_bytes(), 9)).decode("ascii")


def remote_command(snapshot: bool, interpreter: str = REMOTE_PYTHON) -> str:
    """ONE shell command string that runs the probe in the container.

    A single string (not argv) because `railway ssh <cmd>` hands the command to a
    remote shell — the same way `railway ssh -s tenant-x 'ls /opt/data'` works. The
    base64 payload only uses [A-Za-z0-9+/=], so it survives shlex.quote unchanged.
    """
    code = f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{encoded_probe()}')))"
    parts = [interpreter, "-I", "-B", "-c", code]
    if snapshot:
        parts.append("--snapshot")
    return " ".join(shlex.quote(part) for part in parts)


def scope_flags(args: argparse.Namespace) -> list[str]:
    """`-p/-e` are SUBCOMMAND options in railway CLI 5.x (`railway ssh -p ..`,
    `railway variable list -p ..`), not top-level ones."""
    flags = []
    if args.project:
        flags += ["-p", args.project]
    if args.environment:
        flags += ["-e", args.environment]
    return flags


# --------------------------------------------------------------------------------
# running + parsing
# --------------------------------------------------------------------------------
def parse_probe_output(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    raise AuditError("no SQUIRE_AUDIT line in probe output")


def is_gateway_blob(stdout: str) -> bool:
    """Railway's SSH gateway answered instead of the container -> retry."""
    return stdout.lstrip().startswith('{"status":')


def run_probe(service: str, args: argparse.Namespace, runner=subprocess.run) -> dict:
    cmd = ["railway", "ssh", *scope_flags(args), "-s", service, remote_command(args.snapshot)]

    last = "no attempt made"
    for attempt in range(1, args.retries + 1):
        try:
            res = runner(cmd, capture_output=True, text=True, timeout=args.ssh_timeout, check=False)
        except subprocess.TimeoutExpired:
            last = f"railway ssh timed out after {args.ssh_timeout:.0f}s"
        else:
            if is_gateway_blob(res.stdout):
                last = "railway ssh gateway blob (not the container)"
            else:
                try:
                    return parse_probe_output(res.stdout)
                except (AuditError, ValueError) as exc:
                    # Only the first line of stderr: it can be long, and it is ours to show.
                    err = (res.stderr or "").strip().splitlines()[:1]
                    raise AuditError(f"{exc} (rc={res.returncode}; {err})") from exc
        if attempt < args.retries:
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise AuditError(f"gave up after {args.retries} attempts: {last}")


def reduce_railway_vars(raw) -> dict[str, bool]:
    """Keep only whether allowlisted names are set non-empty. Values are dropped here."""
    if isinstance(raw, list):  # tolerate a [{name, value}] shape as well as {name: value}
        raw = {
            str(item.get("name")): item.get("value")
            for item in raw
            if isinstance(item, dict) and "name" in item
        }
    if not isinstance(raw, dict):
        return {}
    flags = {name: bool(raw.get(name)) for name in RAILWAY_VAR_NAMES}
    for prefix in RAILWAY_VAR_PREFIXES:
        flags[prefix + "*"] = any(k.startswith(prefix) and v for k, v in raw.items())
    return flags


def railway_var_flags(service: str, args: argparse.Namespace, runner=subprocess.run):
    cmd = ["railway", "variable", "list", *scope_flags(args), "-s", service, "--json"]
    try:
        res = runner(cmd, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    try:
        return reduce_railway_vars(json.loads(res.stdout))
    except ValueError:
        return None


# --------------------------------------------------------------------------------
# fleet selection + reporting
# --------------------------------------------------------------------------------
def alias_of(email: str) -> str:
    """`me+1ctest7@x.com` -> `+1ctest7`; otherwise the local part."""
    local = email.split("@", 1)[0]
    return "+" + local.split("+", 1)[1] if "+" in local else local


def select(rows: list[dict], wanted: list[str]) -> list[dict]:
    if not wanted:
        return rows
    keys = {w.lstrip("+") for w in wanted}
    picked = [
        r for r in rows
        if r["tenant_id"] in keys or r["email"] in keys or alias_of(r["email"]).lstrip("+") in keys
    ]
    missing = keys - {r["tenant_id"] for r in picked} - {
        alias_of(r["email"]).lstrip("+") for r in picked
    } - {r["email"] for r in picked}
    if missing:
        raise AuditError(f"unknown tenant(s): {', '.join(sorted(missing))}")
    return picked


def flags_for(report: dict) -> list[str]:
    """Short attention markers for the table's last column."""
    out = []
    oauth = report.get("oauth", {})
    rvars = report.get("railway_vars") or {}
    if any(
        oauth.get(k)
        for k in ("claude_credentials_file", "claude_json_file", "anthropic_oauth_file",
                  "process_env_oauth", "dotenv_oauth")
    ) or oauth.get("auth_pool_anthropic") or rvars.get("ANTHROPIC_TOKEN") or rvars.get(
        "CLAUDE_CODE_OAUTH_TOKEN"
    ):
        out.append("OAUTH!")
    bypass = ("HERMES_MANAGED_DIR", "PYTEST_CURRENT_TEST", "HERMES_SAFE_MODE")
    if any(
        report.get("dotenv_flags", {}).get(n) or report.get("process_env_flags", {}).get(n)
        or rvars.get(n)
        for n in bypass
    ):
        out.append("BYPASS!")
    if report.get("no_home_and_no_concierge_state"):
        out.append("NOHOME+NOSTATE")
    gw = report.get("gateway", {})
    if gw.get("argv_replace") or gw.get("s6_gateway_default"):
        out.append("S6-BUG3?")
    if report.get("errors"):
        out.append("ERR:" + ",".join(report["errors"]))
    return out


def table(results: list[dict]) -> str:
    cols = ["alias", "tenant", "image", "cfgv", "home", "owner", "hookOK", "gw", "sup",
            "concierge", "valve", "flags"]
    lines = [cols]
    for res in results:
        r = res.get("report")
        if r is None:
            lines.append([res["alias"], res["tenant_id"], "-", "-", "-", "-", "-", "-", "-",
                          "-", "-", res.get("error", "?")])
            continue
        cfg, gw, cc = r["config"], r["gateway"], r["concierge"]
        image = (r.get("image_ref") or "-").rsplit(":", 1)[-1]
        valve = "-" if cc["valve_count"] is None else f"{cc['valve_state'] or ''}:{cc['valve_count']}"
        lines.append([
            res["alias"], res["tenant_id"], image, str(cfg["config_version"]),
            "y" if cfg["home_channel_present"] else "n",
            {True: "y", False: "NO", None: "?"}[cfg["home_channel_is_owner"]],
            "y" if r["hooks_allowlist"]["concierge_entry"] else "n",
            f"{gw['state'] or '-'}/{'up' if gw['pid_alive'] else 'dead' if gw['pid_alive'] is False else '?'}",
            gw["supervisor_status"] or "-", cc["state"] or "-", valve, " ".join(flags_for(r)),
        ])
    widths = [max(len(str(row[i])) for row in lines) for i in range(len(cols))]
    return "\n".join("  ".join(str(v).ljust(w) for v, w in zip(row, widths)) for row in lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--project", help="Railway project (e.g. squire-staging)")
    ap.add_argument("-e", "--environment", help="Railway environment (e.g. production)")
    ap.add_argument("--tenant", action="append", default=[],
                    help="tenant id, email or +alias; repeatable (default: whole fleet)")
    ap.add_argument("--snapshot", action="store_true",
                    help="also hash .squire/ files + config.yaml (for rollback diffs)")
    ap.add_argument("--out", type=Path, help="write the full JSON results here")
    ap.add_argument("--no-railway-vars", action="store_true",
                    help="skip reading Railway service variable NAMES")
    ap.add_argument("--service-prefix", default="tenant-")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    ap.add_argument("--ssh-timeout", type=float, default=DEFAULT_SSH_TIMEOUT)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, runner=subprocess.run) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        api = ControlAPI()
        resp = api.get("/internal/fleet")
    except ControlAPIError as exc:
        return fail(str(exc))
    if resp.status_code != 200:
        return fail(f"GET /internal/fleet -> HTTP {resp.status_code}")
    try:
        rows = select(resp.json()["tenants"], args.tenant)
    except AuditError as exc:
        return fail(str(exc))

    results, failures = [], 0
    for row in rows:
        res = {"tenant_id": row["tenant_id"], "alias": alias_of(row["email"]),
               "status": row["status"], "fleet_image_ref": row.get("image_ref"),
               "reported_image_ref": row.get("reported_image_ref")}
        if row["status"] != "running":
            res["error"] = f"not probed (status={row['status']})"
            results.append(res)
            continue
        service = args.service_prefix + row["tenant_id"]
        print(f"auditing {res['alias']} ({service}) ...", flush=True)
        try:
            res["report"] = run_probe(service, args, runner=runner)
        except AuditError as exc:
            failures += 1
            res["error"] = str(exc)
        else:
            if not args.no_railway_vars:
                res["report"]["railway_vars"] = railway_var_flags(service, args, runner=runner)
        results.append(res)

    print()
    print(table(results))
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.out}")
    if failures:
        print(f"\n{failures} tenant(s) could not be audited")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
