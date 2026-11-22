import os
import sys
from pathlib import Path
import pytest

SOURCE=Path(os.environ.get("HERMES_SOURCE","/opt/hermes"))
if SOURCE.exists():sys.path.insert(0,str(SOURCE))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"plugin"))

@pytest.fixture
def scope(tmp_path,monkeypatch):
    values={"HERMES_HOME":str(tmp_path),"SQUIRE_RUNTIME_ID":"runtime-a","SQUIRE_OWNER_TENANT_ID":"tenant-a","SQUIRE_CONVERSATION_ID":"conversation-a","SQUIRE_RUNTIME_KIND":"dm","SQUIRE_INGRESS_TOKEN":"i"*40,"SQUIRE_EGRESS_TOKEN":"e"*40,"SQUIRE_CONTROL_TOKEN":"c"*40,"SQUIRE_MESSAGING_URL":"http://messaging.railway.internal:8080","CONTROL_API_URL":"http://control-api.railway.internal:8080","SQUIRE_DEPLOYMENT_GENERATION":"1","SQUIRE_SCOPED_ALLOW_ALL":"true","ANTHROPIC_API_KEY":"test-model-key"}
    for key,value in values.items():monkeypatch.setenv(key,value)
    (tmp_path/"config.yaml").write_text("plugins:\n  enabled: [squire-overlay]\ngroup_sessions_per_user: false\n")
    from squire_overlay.config import RuntimeConfig
    return RuntimeConfig.from_env()

@pytest.fixture
def hermes(scope,monkeypatch):
    if not SOURCE.exists():pytest.skip("exact Hermes source must be supplied with HERMES_SOURCE")
    monkeypatch.setenv("HERMES_MANAGED_DIR",str(Path(__file__).resolve().parents[1]/"managed"))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()
    from hermes_cli.plugins import _reset_plugin_managers_for_tests,discover_plugins,get_plugin_manager
    _reset_plugin_managers_for_tests();discover_plugins(force=True)
    manager=get_plugin_manager()
    yield manager
    _reset_plugin_managers_for_tests()
