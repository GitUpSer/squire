import json
from squire_overlay.guardrails import progress, policy
from squire_overlay.store import Store


def test_progress_is_private_and_bound_to_runtime(scope):
    good={'platform':'squire','chat_id':scope.conversation_id,'session_key':f'agent:main:squire:dm:{scope.conversation_id}'}
    for bad in [dict(good,chat_id='another-customer'),dict(good,platform='whatsapp'),dict(good,session_key='agent:main:squire:group:private')]:
        assert not progress.send_via_hermes(bad,'private progress')['ok']
    assert Store(scope.database).outbox() is None
    assert progress.send_via_hermes(good,'still working')['ok']
    payload=json.loads(Store(scope.database).outbox()['payload'])
    assert payload['conversation_id']==scope.conversation_id
    assert 'recipient' not in payload


def test_guardrails_command_cannot_be_called_from_another_conversation(scope,monkeypatch):
    monkeypatch.setattr(policy,'_current_origin',lambda:{'platform':'squire','chat_id':'different'})
    monkeypatch.setattr(policy,'recover_text',lambda:(_ for _ in ()).throw(AssertionError('must not recover')))
    assert 'private conversation' in policy._cmd_guardrails('recover')


def test_no_systemd_dependency_in_container_policy():
    assert policy.DEFAULTS['resource_scope'] is False


def test_browser_admission_reads_container_memory_limit(tmp_path):
    from squire_overlay.guardrails.preflight import container_available_mb
    (tmp_path/'memory.max').write_text(str(4096*1024*1024))
    (tmp_path/'memory.current').write_text(str(3900*1024*1024))
    assert container_available_mb(tmp_path)==196
    (tmp_path/'memory.max').write_text('max')
    assert container_available_mb(tmp_path) is None
