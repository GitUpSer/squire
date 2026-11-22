"""Tests for `infra/fleet_audit.py` + `infra/fleet_audit_probe.py` (Phase ③ step 0a).

The property that matters most is the privacy contract: the probe runs as root
inside a tenant and reads `.env`, `auth.json` and credential files, so a test plants
real-looking secrets in a fixture tenant tree and asserts NONE of them reach the
output — through the exact base64/zlib/exec command the driver sends over
`railway ssh`, not just by importing the module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
import respx

import fleet_audit
import fleet_audit_probe

API = "https://control-api.squire.test"

# Planted secrets / identifiers that must never appear in probe or driver output.
OAUTH_SECRET = "sk-ant-oat01-PLANTED-OAUTH-SECRET"
PROC_SECRET = "PLANTED-PROC-ANTHROPIC-TOKEN"
API_KEY = "sk-ant-api03-PLANTED-API-KEY"
CHAT_ID = "987654321"
POOL_SECRET = "PLANTED-POOL-REFRESH-TOKEN"
RAILWAY_SECRET = "PLANTED-RAILWAY-VALUE"
SECRETS = (OAUTH_SECRET, PROC_SECRET, API_KEY, CHAT_ID, POOL_SECRET, RAILWAY_SECRET)


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch):
    monkeypatch.setenv("CONTROL_API_URL", API)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")


def write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tenant_tree(tmp_path):
    """A fixture tenant filesystem: AUDIT_ROOT=root, AUDIT_PROC_ROOT=proc."""
    root, proc = tmp_path / "root", tmp_path / "proc"
    home = root / "opt/data"
    write(
        home / "config.yaml",
        f"""\
_config_version: 33
model: gpt-5
platforms:
  telegram:
    home_channel: {{platform: telegram, chat_id: '{CHAT_ID}', name: Home}}
plugins: {{enabled: [some-plugin], disabled: []}}
telegram: {{gateway_restart_notification: false}}
compression: {{codex_gpt55_autoraise_notice: false}}
hooks:
  pre_llm_call:
    - {{command: /opt/squire/bin/squire-concierge-hook.py}}
logging: {{level: WARNING}}
memory: {{provider: hindsight}}
display: {{platforms: {{telegram: {{streaming: true}}}}}}
onboarding: {{profile_build: 'off'}}
""",
    )
    write(home / "platforms/pairing/telegram-approved.json", json.dumps({CHAT_ID: {"name": "x"}}))
    write(
        home / ".env",
        f"OPENAI_API_KEY={API_KEY}\nexport CLAUDE_CODE_OAUTH_TOKEN='{OAUTH_SECRET}'\n"
        "HERMES_MANAGED_DIR=\n# ANTHROPIC_TOKEN=commented-out\n",
    )
    write(
        home / "auth.json",
        json.dumps({"credential_pool": {"anthropic": [{"refresh": POOL_SECRET}]}}),
    )
    write(home / ".claude/.credentials.json", json.dumps({"token": OAUTH_SECRET}))
    write(
        home / "shell-hooks-allowlist.json",
        json.dumps({"approvals": [{"event": "pre_llm_call",
                                   "command": "/opt/squire/bin/squire-concierge-hook.py"}]}),
    )
    write(home / "gateway_state.json",
          json.dumps({"pid": 4242, "argv": ["hermes", "gateway", "run", "--replace"],
                      "gateway_state": "running"}))
    (proc / "4242").mkdir(parents=True)
    write(home / ".squire/concierge-state.json", json.dumps({"state": "connect_llm"}))
    write(home / ".squire/concierge-hook-turns", "connect_llm 3 turn-abc\n")
    write(home / ".squire/initialized", "")
    write(home / ".squire/secrets/.env.enc", "ciphertext")
    (proc / "1").mkdir(parents=True, exist_ok=True)
    (proc / "1" / "environ").write_bytes(
        b"SQUIRE_IMAGE_REF=ghcr.io/shagarwal/squire/hermes-tenant:0.2.12\0"
        + f"ANTHROPIC_TOKEN={PROC_SECRET}\0".encode()
    )
    return root, proc


def run_remote_locally(tenant_tree, snapshot: bool = False) -> subprocess.CompletedProcess:
    """Execute the EXACT command string the driver sends, via a local shell."""
    root, proc = tenant_tree
    cmd = fleet_audit.remote_command(snapshot=snapshot, interpreter=sys.executable)
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, check=True,
        env={"AUDIT_ROOT": str(root), "AUDIT_PROC_ROOT": str(proc), "PATH": "/usr/bin:/bin"},
    )


def test_encoded_remote_command_runs_probe_and_leaks_nothing(tenant_tree):
    pytest.importorskip("yaml")
    res = run_remote_locally(tenant_tree, snapshot=True)
    for secret in SECRETS:
        assert secret not in res.stdout, f"leaked {secret!r}"
        assert secret not in res.stderr
    report = fleet_audit.parse_probe_output(res.stdout)

    assert report["image_ref"] == "ghcr.io/shagarwal/squire/hermes-tenant:0.2.12"
    cfg = report["config"]
    assert cfg["config_version"] == 33
    assert cfg["home_channel_present"] is True
    assert cfg["home_channel_is_owner"] is True
    assert cfg["plugins_enabled"] == ["some-plugin"]
    assert cfg["restart_notification"] is False
    assert cfg["pre_llm_call_hooks"] == 1
    assert cfg["logging_level"] == "WARNING"
    assert cfg["memory_provider"] == "hindsight"
    assert cfg["telegram_streaming"] is True
    assert report["hooks_allowlist"] == {"exists": True, "entries": 1, "concierge_entry": True}
    assert report["gateway"]["pid_alive"] is True
    assert report["gateway"]["argv_replace"] is True
    assert report["concierge"] == {
        "state_file": True, "state": "connect_llm", "valve_state": "connect_llm", "valve_count": 3,
    }
    oauth = report["oauth"]
    assert oauth["claude_credentials_file"] is True
    assert oauth["auth_pool_anthropic"] == 1
    assert oauth["process_env_oauth"] is True
    assert oauth["dotenv_oauth"] is True
    # Present-but-EMPTY and commented-out names do not count as set.
    assert report["dotenv_flags"]["HERMES_MANAGED_DIR"] is False
    assert report["dotenv_flags"]["ANTHROPIC_TOKEN"] is False
    assert report["no_home_and_no_concierge_state"] is False
    # Snapshot: hashes only, secrets/ excluded.
    assert set(report["snapshot"]) == {
        "config.yaml", ".squire/concierge-state.json", ".squire/concierge-hook-turns",
        ".squire/initialized",
    }
    assert all(len(v) == 64 for v in report["snapshot"].values())


def test_unexpected_values_are_masked_not_echoed(tenant_tree):
    root, _ = tenant_tree
    home = root / "opt/data"
    write(home / ".squire/concierge-state.json", json.dumps({"state": "x'); DROP TABLE t;--"}))
    write(home / "config.yaml", "logging: {level: 'my secret value'}\nplugins: {enabled: ['a b c']}\n")
    report = fleet_audit.parse_probe_output(run_remote_locally(tenant_tree).stdout)
    assert report["concierge"]["state"] == fleet_audit_probe.INVALID
    if report["config"]["logging_level"] is not None:  # None only without PyYAML
        assert report["config"]["logging_level"] == fleet_audit_probe.INVALID
        assert report["config"]["plugins_enabled"] == [fleet_audit_probe.INVALID]


@pytest.mark.parametrize(
    ("valve", "expected"),
    [("7\n", (None, 7)), ("greet 2\n", ("greet", 2)), ("", (None, None))],
)
def test_legacy_valve_formats(tenant_tree, valve, expected):
    root, _ = tenant_tree
    write(root / "opt/data/.squire/concierge-hook-turns", valve)
    report = fleet_audit.parse_probe_output(run_remote_locally(tenant_tree).stdout)
    assert (report["concierge"]["valve_state"], report["concierge"]["valve_count"]) == expected


def test_empty_tenant_reports_no_home_and_no_state(tmp_path):
    (tmp_path / "root/opt/data").mkdir(parents=True)
    (tmp_path / "proc").mkdir()
    report = fleet_audit.parse_probe_output(
        run_remote_locally((tmp_path / "root", tmp_path / "proc")).stdout
    )
    assert report["config"]["exists"] is False
    assert report["no_home_and_no_concierge_state"] is True
    assert report["image_ref"] is None


# ---------------------------------------------------------------------------- driver
def args(**kw):
    base = dict(project="squire-staging", environment="production", snapshot=False,
                retries=3, ssh_timeout=5.0)
    base.update(kw)
    return SimpleNamespace(**base)


def fake_runner(outputs):
    """Returns queued CompletedProcess-likes; records the commands it was given."""
    calls = []

    def runner(cmd, **_kw):
        calls.append(cmd)
        out = outputs.pop(0)
        return SimpleNamespace(stdout=out, stderr="", returncode=0)

    runner.calls = calls
    return runner


def test_run_probe_retries_railway_gateway_blob(monkeypatch):
    monkeypatch.setattr(fleet_audit.time, "sleep", lambda _s: None)
    good = 'SQUIRE_AUDIT {"probe_version": 1}'
    runner = fake_runner(['{"status":"ready","account":{}}', good])
    assert fleet_audit.run_probe("tenant-t1", args(), runner=runner) == {"probe_version": 1}
    assert len(runner.calls) == 2
    cmd = runner.calls[0]
    assert cmd[:6] == ["railway", "ssh", "-p", "squire-staging", "-e", "production"]
    assert cmd[6:8] == ["-s", "tenant-t1"]
    assert cmd[8].startswith("/opt/hermes/.venv/bin/python -I -B -c ")


def test_run_probe_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(fleet_audit.time, "sleep", lambda _s: None)
    runner = fake_runner(['{"status":"unavailable"}'] * 3)
    with pytest.raises(fleet_audit.AuditError, match="gave up after 3"):
        fleet_audit.run_probe("tenant-t1", args(), runner=runner)


def test_run_probe_non_blob_garbage_is_a_hard_failure():
    runner = fake_runner(["bash: python: not found"])
    with pytest.raises(fleet_audit.AuditError, match="no SQUIRE_AUDIT line"):
        fleet_audit.run_probe("tenant-t1", args(), runner=runner)
    assert len(runner.calls) == 1


def test_railway_vars_reduced_to_names_only():
    raw = {"ANTHROPIC_TOKEN": RAILWAY_SECRET, "HERMES_SAFE_MODE": "",
           "HINDSIGHT_API_EMBEDDINGS_KEY": RAILWAY_SECRET, "OTHER": RAILWAY_SECRET}
    flags = fleet_audit.reduce_railway_vars(raw)
    assert flags["ANTHROPIC_TOKEN"] is True
    assert flags["HERMES_SAFE_MODE"] is False
    assert flags["HINDSIGHT_API_EMBEDDINGS_*"] is True
    assert "OTHER" not in flags
    assert RAILWAY_SECRET not in json.dumps(flags)
    # list-of-{name,value} shape too
    assert fleet_audit.reduce_railway_vars([{"name": "ANTHROPIC_TOKEN", "value": "x"}])[
        "ANTHROPIC_TOKEN"
    ] is True


def test_select_by_alias_id_and_email():
    rows = [{"tenant_id": "t1", "email": "me+1ctest7@x.com"},
            {"tenant_id": "t2", "email": "other@x.com"}]
    assert [r["tenant_id"] for r in fleet_audit.select(rows, ["+1ctest7"])] == ["t1"]
    assert [r["tenant_id"] for r in fleet_audit.select(rows, ["t2"])] == ["t2"]
    assert [r["tenant_id"] for r in fleet_audit.select(rows, ["other@x.com"])] == ["t2"]
    with pytest.raises(fleet_audit.AuditError, match="unknown tenant"):
        fleet_audit.select(rows, ["nope"])


@respx.mock
def test_main_end_to_end_writes_clean_json(tenant_tree, tmp_path, monkeypatch, capsys):
    pytest.importorskip("yaml")
    root, proc = tenant_tree
    monkeypatch.setenv("AUDIT_ROOT", str(root))
    monkeypatch.setenv("AUDIT_PROC_ROOT", str(proc))
    respx.get(f"{API}/internal/fleet").mock(return_value=httpx.Response(200, json={
        "summary": {},
        "tenants": [
            {"tenant_id": "t1", "email": "me+1ctest7@x.com", "status": "running",
             "image_ref": "img:0.2.12", "reported_image_ref": "img:0.2.12"},
            {"tenant_id": "t2", "email": "me+old@x.com", "status": "stopped"},
        ],
    }))

    def runner(cmd, **_kw):
        if cmd[1] == "ssh":
            # Run the real remote command locally against the fixture tree.
            remote = cmd[-1].replace(fleet_audit.REMOTE_PYTHON, sys.executable, 1)
            return subprocess.run(remote, shell=True, capture_output=True, text=True, check=False)
        return SimpleNamespace(stdout=json.dumps({"ANTHROPIC_TOKEN": RAILWAY_SECRET}),
                               stderr="", returncode=0)

    out = tmp_path / "audit.json"
    assert fleet_audit.main(["--out", str(out)], runner=runner) == 0
    printed = capsys.readouterr().out
    written = out.read_text()
    for secret in SECRETS:
        assert secret not in printed and secret not in written, f"leaked {secret!r}"
    results = json.loads(written)
    assert results[0]["report"]["railway_vars"]["ANTHROPIC_TOKEN"] is True
    assert "not probed" in results[1]["error"]
    assert "OAUTH!" in printed and "S6-BUG3?" in printed
