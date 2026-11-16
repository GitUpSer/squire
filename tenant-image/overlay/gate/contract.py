#!/usr/bin/env python3
"""Build-failing contract against the installed, real pinned Hermes source."""
import inspect
import json
import os
from pathlib import Path
import tempfile


def main():
    import hermes_cli
    assert hermes_cli.__version__=="0.21.5", "unexpected Hermes runtime version"
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent
    from hermes_cli.plugins import PluginContext
    assert "adapter_factory" in inspect.signature(PluginContext.register_platform).parameters
    assert "outcome" in inspect.signature(BasePlatformAdapter.on_processing_complete).parameters
    assert "_gateway_accepted" in MessageEvent.__dataclass_fields__
    managed=Path(os.environ.get("HERMES_MANAGED_DIR","/etc/hermes"))
    assert (managed/"config.yaml").is_file(), "managed config absent"
    if os.getuid()!=0 and managed==Path("/etc/hermes"):
        assert not os.access(managed/"config.yaml",os.W_OK), "managed config is runtime-writable"
    with tempfile.TemporaryDirectory(prefix="squire-gate-") as directory:
        os.environ.update({"HERMES_HOME":directory,"SQUIRE_RUNTIME_ID":"build-contract","SQUIRE_RUNTIME_KIND":"group","SQUIRE_OWNER_TENANT_ID":"build-sponsor","SQUIRE_CONVERSATION_ID":"build-conversation","SQUIRE_INGRESS_TOKEN":"i"*40,"SQUIRE_EGRESS_TOKEN":"e"*40,"SQUIRE_CONTROL_TOKEN":"c"*40,"SQUIRE_MESSAGING_URL":"http://messaging.railway.internal:8080","CONTROL_API_URL":"http://control-api.railway.internal:8080","SQUIRE_SCOPED_ALLOW_ALL":"true"})
        Path(directory,"config.yaml").write_text("plugins:\n  enabled: []\nlogging:\n  level: DEBUG\n")
        for key in ("PYTEST_CURRENT_TEST","HERMES_SAFE_MODE"):os.environ.pop(key,None)
        from hermes_cli import managed_scope
        managed_scope.invalidate_managed_cache()
        from hermes_cli.plugins import _reset_plugin_managers_for_tests,discover_plugins,get_plugin_manager,get_pre_tool_call_directive
        _reset_plugin_managers_for_tests();discover_plugins(force=True)
        plugin=get_plugin_manager()._plugins.get("squire-overlay")
        assert plugin and plugin.enabled and plugin.error is None,getattr(plugin,"error","not discovered")
        from hermes_cli.config import load_config_readonly
        config=load_config_readonly()
        assert "squire-overlay" in config["plugins"]["enabled"]
        assert config["logging"]["level"]=="WARNING"
        from gateway.config import Platform,PlatformConfig,load_gateway_config
        from gateway.platform_registry import platform_registry
        from gateway.session import SessionSource,build_session_key
        from squire_overlay.adapter import SquireAdapter
        assert platform_registry.is_registered("squire")
        gateway_config=load_gateway_config()
        assert gateway_config.platforms[Platform("squire")].enabled,"Squire platform not enabled by scoped runtime configuration"
        adapter=SquireAdapter(PlatformConfig(extra={"group_sessions_per_user":False}))
        source=SessionSource(platform=Platform("squire"),chat_id="build-conversation",chat_type="group",user_id="member")
        assert build_session_key(source,group_sessions_per_user=False)=="agent:main:squire:group:build-conversation"
        for tool in ("terminal","browser","read_file","send_message","delegate_task"):
            assert get_pre_tool_call_directive(tool,{})[0]=="block",tool
        assert adapter.toolsets_for_source(source)==["memory","squire-group"]
        from hermes_cli.plugin_compat import scan_plugin
        import squire_overlay
        assert scan_plugin(Path(squire_overlay.__file__).parent)==[],"plugin imports unsupported moved APIs"
        print(json.dumps({"hermes":"0.21.5","overlay":"0.4.0","platform":"squire","managed":True,"group_policy":True}))

if __name__=="__main__":main()
