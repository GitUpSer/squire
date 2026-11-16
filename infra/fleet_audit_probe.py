"""Read-only per-tenant audit probe. Runs INSIDE a tenant container. **Phase ③ step 0a.**

This file is never installed in the image. `infra/fleet_audit.py` compresses it,
base64-encodes it and runs it through `railway ssh` with the image's own interpreter:

    /opt/hermes/.venv/bin/python -I -B -c "import base64,zlib;exec(zlib.decompress(...))"

It prints exactly ONE line, `SQUIRE_AUDIT {json}`, which the laptop-side driver parses.

WHAT IT MAY PRINT (the privacy contract — pinned by test_fleet_audit.py)
-----------------------------------------------------------------------
Only booleans, small integers, hashes and tokens that pass a strict allowlist regex:
state names, config-version numbers, plugin names, a log level. NEVER a value from
`.env`, `auth.json` or any credential file, never a chat id, bot token or user name.
For secrets the probe reports *presence* (`true`/`false`) or a *count*, nothing more.
A field whose raw value fails its regex is reported as the literal "<invalid>" rather
than echoed — so a surprising value can never smuggle content out.

WHY `-I -B`
----------
`railway ssh` lands as root. `-B` stops Python writing `__pycache__` and `-I` ignores
user site/env, so the probe leaves no root-owned files the gateway (uid hermes) would
later trip over. The probe itself never writes anything.

Testing: every absolute path is resolved under `AUDIT_ROOT` (default: `/`) and every
`/proc` lookup under `AUDIT_PROC_ROOT` (default: `/proc`), so the test suite can run
the probe against a fixture tree. `--snapshot` adds sha256s of `.squire/` files for
the rollback-rehearsal diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

PROBE_VERSION = 1

# --- test seams -------------------------------------------------------------------
ROOT = os.environ.get("AUDIT_ROOT", "/")
PROC = os.environ.get("AUDIT_PROC_ROOT", "/proc")

# Tenant layout (tenant-image/bin/squire-entrypoint.sh): HERMES_HOME=/opt/data,
# HOME is the same volume, state under .squire/, the decrypted .env / auth.json live
# on tmpfs behind symlinks in HERMES_HOME.
HERMES_HOME = "/opt/data"
SUPERVISORCTL = "/opt/squire/venv/bin/supervisorctl"
SUPERVISORD_CONF = "/opt/squire/supervisord.conf"

# --- allowlist regexes for anything that is not a bool / int ----------------------
STATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")          # concierge / gateway states
PLUGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$")
LEVEL_RE = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$", re.I)
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9./:@_-]{0,199}$")
SUPERVISOR_RE = re.compile(r"^[A-Z]{3,12}$")
SQUIRE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: `.env` / process-env names whose PRESENCE we report. Values are never read out.
ENV_FLAG_NAMES = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_TOKEN",
    "HERMES_MANAGED_DIR",
    "PYTEST_CURRENT_TEST",
    "HERMES_SAFE_MODE",
    "TERMINAL_TIMEOUT",
)

INVALID = "<invalid>"


def p(path: str) -> str:
    """Resolve an absolute container path under the (test-overridable) root."""
    return os.path.join(ROOT, path.lstrip("/"))


def safe(value, pattern: re.Pattern):
    """Return `value` only if it is a string matching `pattern`; None stays None."""
    if value is None:
        return None
    if isinstance(value, str) and pattern.match(value):
        return value
    return INVALID


def sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_yaml(path: str, errors: list[str]):
    """Parse config.yaml. PyYAML ships in the hermes venv; degrade if it does not."""
    try:
        import yaml  # noqa: PLC0415 - optional dependency, imported lazily on purpose
    except ImportError:
        errors.append("no_pyyaml")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError:
        return None
    except Exception:  # noqa: BLE001 - a malformed config is itself a finding
        errors.append("config_unparseable")
        return None
    return data if isinstance(data, dict) else None


def dig(data, *keys):
    """Nested dict lookup that tolerates any non-dict along the way."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def as_bool(value):
    return value if isinstance(value, bool) else None


def env_names(path: str) -> dict[str, bool]:
    """Which ENV_FLAG_NAMES are set NON-EMPTY in a dotenv file. Values are discarded."""
    found = {name: False for name in ENV_FLAG_NAMES}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return found
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name in found and value.strip().strip("'\""):
            found[name] = True
    return found


def proc_environ(pid: int) -> dict[str, str]:
    try:
        with open(os.path.join(PROC, str(pid), "environ"), "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def pid_alive(pid) -> bool | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    return os.path.isdir(os.path.join(PROC, str(pid)))


def audit_config(home: str, errors: list[str]) -> tuple[dict, dict | None]:
    path = p(f"{home}/config.yaml")
    out: dict = {"exists": os.path.isfile(path), "sha256": sha256_file(path)}
    cfg = load_yaml(path, errors) if out["exists"] else None
    tg_home = dig(cfg, "platforms", "telegram", "home_channel")
    enabled = dig(cfg, "plugins", "enabled")
    disabled = dig(cfg, "plugins", "disabled")
    hooks = dig(cfg, "hooks", "pre_llm_call")
    version = dig(cfg, "_config_version")
    out.update(
        {
            "config_version": version if isinstance(version, int) else None,
            "home_channel_present": isinstance(tg_home, dict) and bool(tg_home.get("chat_id")),
            "plugins_enabled": (
                [safe(x, PLUGIN_RE) for x in enabled] if isinstance(enabled, list) else None
            ),
            "plugins_disabled": (
                [safe(x, PLUGIN_RE) for x in disabled] if isinstance(disabled, list) else None
            ),
            "restart_notification": as_bool(dig(cfg, "telegram", "gateway_restart_notification")),
            "codex_autoraise_notice": as_bool(
                dig(cfg, "compression", "codex_gpt55_autoraise_notice")
            ),
            "pre_llm_call_hooks": len(hooks) if isinstance(hooks, list) else 0,
            "hooks_auto_accept": as_bool(dig(cfg, "hooks_auto_accept")),
            "logging_level": safe(dig(cfg, "logging", "level"), LEVEL_RE),
            "memory_provider": safe(dig(cfg, "memory", "provider"), PROVIDER_RE),
            "telegram_streaming": as_bool(
                dig(cfg, "display", "platforms", "telegram", "streaming")
            ),
            "whatsapp_platform_present": dig(cfg, "platforms", "whatsapp") is not None,
            "onboarding_profile_build": safe(
                dig(cfg, "onboarding", "profile_build"), PROVIDER_RE
            ),
        }
    )
    return out, tg_home if isinstance(tg_home, dict) else None


def owner_ids(home: str) -> set[str]:
    """Approved Telegram owner ids — only ever compared, never printed."""
    for rel in ("platforms/pairing/telegram-approved.json", "pairing/telegram-approved.json"):
        data = read_json(p(f"{home}/{rel}"))
        if isinstance(data, dict):
            return {str(k) for k in data}
        if isinstance(data, list):
            return {str(x.get("user_id", x)) if isinstance(x, dict) else str(x) for x in data}
    return set()


def audit_hooks_allowlist(home: str) -> dict:
    # Upstream format (agent/shell_hooks.py load_allowlist / _is_allowlisted):
    #   {"approvals": [{"event": "pre_llm_call", "command": "<path>", ...}, ...]}
    data = read_json(p(f"{home}/shell-hooks-allowlist.json"))
    entries = data.get("approvals") if isinstance(data, dict) else None
    entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    return {
        "exists": data is not None,
        "entries": len(entries),
        # Consent persists per (event, command): this entry is why v0.3.0 must keep an
        # inert stub at the old concierge path (plan D3).
        "concierge_entry": any(
            e.get("event") == "pre_llm_call"
            and str(e.get("command", "")).endswith("squire-concierge-hook.py")
            for e in entries
        ),
    }


def audit_gateway(home: str) -> dict:
    state = read_json(p(f"{home}/gateway_state.json"))
    state = state if isinstance(state, dict) else {}
    argv = state.get("argv") if isinstance(state.get("argv"), list) else []
    out = {
        "state_file": bool(state),
        "state": safe(state.get("gateway_state"), STATE_RE),
        "pid_alive": pid_alive(state.get("pid")),
        # `--replace` in the recorded argv is the s6 gateway-default signature (memory:
        # connect-gateway-restart-fatal "Bug 3"): supervisord's gateway lost the race.
        "argv_replace": "--replace" in argv,
        "s6_gateway_default": os.path.exists(p("/run/service/gateway-default")),
        "supervisor_status": None,
    }
    if ROOT == "/" and os.path.exists(SUPERVISORCTL):
        try:
            res = subprocess.run(
                [SUPERVISORCTL, "-c", SUPERVISORD_CONF, "status", "gateway"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            parts = res.stdout.split()
            out["supervisor_status"] = safe(parts[1] if len(parts) > 1 else None, SUPERVISOR_RE)
        except (OSError, subprocess.SubprocessError):
            out["supervisor_status"] = "<error>"
    return out


def audit_concierge(home: str) -> dict:
    state_dir = f"{home}/.squire"
    state = read_json(p(f"{state_dir}/concierge-state.json"))
    out = {
        "state_file": state is not None,
        "state": safe(state.get("state") if isinstance(state, dict) else None, STATE_RE),
        "valve_state": None,
        "valve_count": None,
    }
    # Valve format: "state count turn_id" (legacy: "state count" or a bare integer).
    try:
        with open(p(f"{state_dir}/concierge-hook-turns"), encoding="utf-8") as fh:
            fields = fh.read().split()
    except OSError:
        fields = []
    if len(fields) == 1 and fields[0].isdigit():
        out["valve_count"] = int(fields[0])
    elif len(fields) >= 2:
        out["valve_state"] = safe(fields[0], STATE_RE)
        out["valve_count"] = int(fields[1]) if fields[1].isdigit() else None
    return out


def audit_oauth(home: str, process_env: dict[str, str]) -> dict:
    """Every Anthropic OAuth source upstream 8.3 auto-discovers (patch-001 drop gate)."""
    auth = read_json(p(f"{home}/auth.json"))
    pool = dig(auth, "credential_pool", "anthropic")
    return {
        "claude_credentials_file": os.path.isfile(p(f"{home}/.claude/.credentials.json")),
        "claude_json_file": os.path.isfile(p(f"{home}/.claude.json")),
        "anthropic_oauth_file": os.path.isfile(p(f"{home}/.anthropic_oauth.json")),
        "auth_pool_anthropic": len(pool) if isinstance(pool, (list, dict)) else 0,
        "process_env_oauth": any(
            process_env.get(n) for n in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_TOKEN")
        ),
    }


def snapshot(home: str) -> dict:
    """sha256 of each top-level .squire/ file (secrets/ excluded) + config.yaml."""
    out = {"config.yaml": sha256_file(p(f"{home}/config.yaml"))}
    state_dir = p(f"{home}/.squire")
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        names = []
    for name in names:
        full = os.path.join(state_dir, name)
        if SQUIRE_FILE_RE.match(name) and os.path.isfile(full):
            out[f".squire/{name}"] = sha256_file(full)
    return out


def main(argv: list[str]) -> int:
    errors: list[str] = []
    home = HERMES_HOME

    # PID 1's environment is what Railway injected (SQUIRE_IMAGE_REF, service vars);
    # fall back to this session's env if /proc/1/environ is unreadable.
    pid1_env = proc_environ(1)
    env_source = "proc1" if pid1_env else "session"
    process_env = pid1_env or dict(os.environ)

    config, tg_home = audit_config(home, errors)
    owners = owner_ids(home)
    config["home_channel_is_owner"] = (
        str(tg_home.get("chat_id")) in owners if tg_home and owners else None
    )

    dotenv = env_names(p(f"{home}/.env"))
    report = {
        "probe_version": PROBE_VERSION,
        "image_ref": safe(process_env.get("SQUIRE_IMAGE_REF") or None, IMAGE_RE),
        "env_source": env_source,
        "config": config,
        "hooks_allowlist": audit_hooks_allowlist(home),
        "gateway": audit_gateway(home),
        "concierge": audit_concierge(home),
        "oauth": audit_oauth(home, process_env),
        "dotenv_flags": dotenv,
        "process_env_flags": {n: bool(process_env.get(n)) for n in ENV_FLAG_NAMES},
        "etc_hermes_exists": os.path.isdir(p("/etc/hermes")),
        "init_marker": os.path.isfile(p(f"{home}/.squire/initialized")),
        "errors": errors,
    }
    report["oauth"]["dotenv_oauth"] = dotenv["CLAUDE_CODE_OAUTH_TOKEN"] or dotenv["ANTHROPIC_TOKEN"]
    report["no_home_and_no_concierge_state"] = (
        not config["home_channel_present"] and not report["concierge"]["state_file"]
    )
    if "--snapshot" in argv:
        report["snapshot"] = snapshot(home)

    print("SQUIRE_AUDIT " + json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    # Under `python -c "exec(...)"` the exec'd code runs with __name__ == "__main__",
    # so the remote run takes this branch; importing the module in tests does not.
    sys.exit(main(sys.argv[1:]))
