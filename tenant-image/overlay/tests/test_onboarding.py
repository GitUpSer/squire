import json
import yaml
from squire_overlay.onboarding import context


def test_pilot_bootstrap_replaces_obsolete_sales_facts_and_hook(scope):
    from squire_overlay.bootstrap import prepare
    (scope.home/'config.yaml').write_text(yaml.safe_dump({'hooks':{'pre_llm_call':'old-concierge'},'memory':{'provider':'hindsight'}}))
    (scope.home/'SOUL.md').write_text('Existing owner identity')
    prepare()
    assert yaml.safe_load((scope.home/'config.yaml').read_text())['hooks']=={}
    assert (scope.home/'SOUL.md').read_text()=='Existing owner identity'
    facts=(scope.home/'skills/concierge/state-machine.yaml').read_text()
    assert '$5/month' not in facts and '$10/month' not in facts
    assert 'first accepted' in facts and 'in-flight' in facts
    assert json.loads((scope.home/'.squire/concierge-state.json').read_text())['state']=='complete'


def test_founder_policy_preserves_nontrial_identity(monkeypatch):
    monkeypatch.setenv('SQUIRE_RESTORED_HOME','true')
    assert 'no trial expiry' in context()['context']
    assert 'do not restart onboarding' in context()['context']


def test_actual_hermes_plugin_delivers_pilot_policy(hermes,scope):
    from hermes_cli.lifecycle import invoke_hook
    results=invoke_hook('pre_llm_call',platform='squire',session_id='owner-session',user_message='How much does it cost?')
    assert any(isinstance(item,dict) and 'no published paid pilot subscription' in item.get('context','') for item in results)
