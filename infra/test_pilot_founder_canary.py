"""Safety boundaries for the single-purpose founder memory restore."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("founder_canary", Path(__file__).with_name("pilot_founder_canary.py"))
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)
IMAGE = "ghcr.io/example/founder@sha256:" + "a" * 64


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    monkeypatch.setattr(canary, "HOME", tmp_path)
    monkeypatch.setattr(canary, "PRIVATE", tmp_path / ".squire/canary")
    monkeypatch.setattr(canary.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(canary.importlib.metadata, "version", lambda name: {"hindsight-api-slim": "0.5.2", "pg0-embedded": "0.12.3"}[name])
    for key in list(canary.os.environ):
        monkeypatch.delenv(key)
    values = {"RAILWAY_SERVICE_ID": canary.SERVICE, "SQUIRE_RUNTIME_ID": canary.RUNTIME, "SQUIRE_RUNTIME_KIND": "dm", "SQUIRE_HINDSIGHT_VARIANT": "founder", "SQUIRE_IMAGE_REF": IMAGE, "HINDSIGHT_API_WORKER_ENABLED": "false", "HINDSIGHT_API_LLM_PROVIDER": "none", "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path)}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@pytest.mark.parametrize("key,value,code", [
    ("RAILWAY_SERVICE_ID", "live-source", "wrong_canary_service"),
    ("SQUIRE_RUNTIME_ID", "founder-live", "wrong_canary_runtime"),
    ("SQUIRE_RUNTIME_KIND", "group", "private_canary_only"),
    ("SQUIRE_HINDSIGHT_VARIANT", "standard", "founder_image_required"),
    ("HINDSIGHT_API_WORKER_ENABLED", "true", "memory_worker_must_be_disabled"),
    ("HINDSIGHT_API_LLM_PROVIDER", "anthropic", "memory_model_must_be_disabled"),
    ("ANTHROPIC_API_KEY", "private-synthetic-key", "external_credentials_forbidden"),
])
def test_exact_scope_and_no_active_credentials(guarded, monkeypatch, key, value, code):
    monkeypatch.setenv(key, value)
    with pytest.raises(canary.CanaryError, match=code):
        canary.guard(IMAGE)


def test_no_owner_auth_file(guarded):
    (guarded / "auth.json").write_text('{"synthetic":"credential"}')
    with pytest.raises(canary.CanaryError, match="owner_credentials_forbidden"):
        canary.guard(IMAGE)


def test_requires_exact_digest(guarded):
    with pytest.raises(canary.CanaryError, match="wrong_immutable_image"):
        canary.guard("ghcr.io/example/founder:latest")


def test_clean_scope_is_accepted(guarded):
    canary.guard(IMAGE)
    assert canary.PRIVATE.stat().st_mode & 0o777 == 0o700


def test_existing_database_is_never_overwritten(tmp_path, monkeypatch):
    archive = tmp_path / "dump"
    archive.write_bytes(b"synthetic archive")
    monkeypatch.setattr(canary, "DUMP_SHA", hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(canary, "verify_server", lambda: None)
    statements = []
    def sql(statement, database=canary.DATABASE):
        statements.append(statement)
        return "1"
    monkeypatch.setattr(canary, "sql", sql)
    with pytest.raises(canary.CanaryError, match="restore_database_exists_no_overwrite"):
        canary.restore(archive)
    assert len(statements) == 1
    assert statements[0].startswith("SELECT 1 FROM pg_database")


def test_wrong_dump_rejected_before_database_query(tmp_path, monkeypatch):
    archive = tmp_path / "dump"
    archive.write_bytes(b"wrong archive")
    monkeypatch.setattr(canary, "verify_server", lambda: None)
    monkeypatch.setattr(canary, "sql", lambda *_: pytest.fail("must not query database"))
    with pytest.raises(canary.CanaryError, match="dump_checksum_mismatch"):
        canary.restore(archive)


def test_content_mutation_cannot_pass_review(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "PRIVATE", tmp_path)
    (tmp_path / "restored-baseline.json").write_text(json.dumps({"tables": {"public.memory_units": {"rows": 5, "full_rows_md5": "old"}}}))
    monkeypatch.setattr(canary, "inventory", lambda: {"tables": {"public.memory_units": {"rows": 5, "full_rows_md5": "changed"}}})
    with pytest.raises(canary.CanaryError, match="database_content_or_schema_changed_after_recall"):
        canary.review()


@pytest.mark.parametrize("argv", [
    [b"/opt/hermes/.venv/bin/hermes", b"gateway", b"run", b"--no-supervise"],
    [b"python", b"-m", b"gateway.run"],
    [b"bash", b"/opt/squire/bin/squire-gateway-run.sh"],
])
def test_real_gateway_command_shapes_are_rejected(argv):
    assert canary.gateway_argv(argv)


def test_readonly_helper_command_is_not_gateway():
    assert not canary.gateway_argv([b"python", b"pilot_founder_canary.py", b"inventory"])


def test_managed_profile_exact_native_defaults_and_isolated_path(monkeypatch):
    for key in ['INSTANCE','DATABASE','PORT','DATA','PRIVATE','USERNAME','PROFILE']:
        monkeypatch.setattr(canary,key,getattr(canary,key))
    canary.activate_profile('embedded-managed')
    assert canary.INSTANCE == 'squire-founder-managed'
    assert canary.DATABASE == canary.USERNAME == 'hindsight'
    assert canary.PORT == 55432
    assert str(canary.DATA) == '/opt/data/.pg0/instances/squire-founder-managed/data'
    assert str(canary.PRIVATE) == '/opt/data/.squire/founder-canary/managed'
    canary.activate_profile('reference')
    assert canary.DATABASE == 'squire_founder_canary'
    assert canary.PORT == 55431


def test_no_arbitrary_cluster_profile():
    with pytest.raises(canary.CanaryError,match='unknown_isolated_profile'):
        canary.activate_profile('production')


def test_reference_cluster_cannot_be_stopped_by_managed_handoff(monkeypatch):
    monkeypatch.setattr(canary,'PROFILE','reference')
    monkeypatch.setattr(canary,'review',lambda:pytest.fail('must reject before inspection'))
    with pytest.raises(canary.CanaryError,match='only_managed_cluster_may_be_stopped'):
        canary.stop_managed()


def test_managed_profile_cannot_start_oneoff_api(monkeypatch):
    monkeypatch.setattr(canary,'PROFILE','embedded-managed')
    monkeypatch.setattr(canary,'inventory',lambda:pytest.fail('must reject before DB access'))
    with pytest.raises(canary.CanaryError,match='managed_profile_must_use_normal_hindsight_binary'):
        canary.serve()


def test_sql_default_database_follows_active_profile(monkeypatch,tmp_path):
    monkeypatch.setattr(canary,'DATABASE','hindsight')
    monkeypatch.setattr(canary,'binaries',lambda:tmp_path)
    monkeypatch.setattr(canary,'connection',lambda database:{'database':database})
    monkeypatch.setattr(canary,'run',lambda argv,env:env['database'])
    assert canary.sql('SELECT 1') == 'hindsight'


def test_adopted_managed_read_allows_only_generated_webhook_field(guarded,monkeypatch):
    monkeypatch.setattr(canary,'PROFILE','embedded-managed')
    (guarded/'.squire').mkdir();(guarded/'.squire/initialized').touch()
    (guarded/'.env').write_text('TELEGRAM_WEBHOOK_SECRET='+'a'*64+'\n')
    canary.guard(IMAGE,allow_managed_read=True)
    with pytest.raises(canary.CanaryError,match='owner_credentials_forbidden'):
        canary.guard(IMAGE)
    (guarded/'.env').write_text('TELEGRAM_WEBHOOK_SECRET='+'a'*64+'\nANTHROPIC_API_KEY=synthetic\n')
    with pytest.raises(canary.CanaryError,match='owner_credentials_forbidden'):
        canary.guard(IMAGE,allow_managed_read=True)


def test_generated_field_exception_requires_native_adoption(guarded,monkeypatch):
    monkeypatch.setattr(canary,'PROFILE','embedded-managed')
    (guarded/'.env').write_text('TELEGRAM_WEBHOOK_SECRET='+'a'*64)
    with pytest.raises(canary.CanaryError,match='owner_credentials_forbidden'):
        canary.guard(IMAGE,allow_managed_read=True)


def test_gateway_read_exception_requires_managed_adoption(guarded,monkeypatch,tmp_path):
    proc=tmp_path/'fake-proc';(proc/'42').mkdir(parents=True)
    (proc/'42/cmdline').write_bytes(b'/opt/hermes/.venv/bin/hermes\0gateway\0run\0')
    real_path=Path
    monkeypatch.setattr(canary,'Path',lambda p:proc if p=='/proc' else real_path(p))
    monkeypatch.setattr(canary,'PROFILE','embedded-managed')
    with pytest.raises(canary.CanaryError,match='gateway_must_remain_stopped'):
        canary.guard(IMAGE,allow_managed_read=True)
    (guarded/'.squire').mkdir();(guarded/'.squire/initialized').touch()
    canary.guard(IMAGE,allow_managed_read=True)
    monkeypatch.setattr(canary,'PROFILE','reference')
    with pytest.raises(canary.CanaryError,match='gateway_must_remain_stopped'):
        canary.guard(IMAGE,allow_managed_read=True)


def test_managed_metadata_never_archived_while_process_alive(monkeypatch,tmp_path):
    monkeypatch.setattr(canary,'PROFILE','embedded-managed')
    monkeypatch.setattr(canary,'USERNAME','hindsight')
    data=tmp_path/'instance/data';data.mkdir(parents=True)
    monkeypatch.setattr(canary,'DATA',data)
    metadata=data.parent/'instance.json'
    metadata.write_text(json.dumps({'data_dir':str(data),'username':'hindsight','database':'postgres','pid':canary.os.getpid()}))
    with pytest.raises(canary.CanaryError,match='managed_pid_is_not_stopped'):
        canary.finalize_managed_stop()
    assert metadata.exists()


def test_managed_metadata_never_archived_after_unclean_shutdown(monkeypatch,tmp_path):
    monkeypatch.setattr(canary,'PROFILE','embedded-managed')
    monkeypatch.setattr(canary,'USERNAME','hindsight')
    monkeypatch.setattr(canary,'HOME',tmp_path)
    data=tmp_path/'.pg0/instances/managed/data';data.mkdir(parents=True)
    monkeypatch.setattr(canary,'DATA',data)
    installation=tmp_path/'.pg0/installation';installation.mkdir();(installation/'pg_controldata').touch()
    metadata=data.parent/'instance.json'
    metadata.write_text(json.dumps({'data_dir':str(data),'username':'hindsight','database':'postgres','pid':99999999,'installation_dir':str(installation)}))
    monkeypatch.setattr(canary,'run',lambda *args:'Database cluster state: in production')
    with pytest.raises(canary.CanaryError,match='managed_cluster_not_cleanly_shutdown'):
        canary.finalize_managed_stop()
    assert metadata.exists()
