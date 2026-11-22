"""Real LiteLLM 1.94.2 accounting functions; no upstream API or paid tokens."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime

import pytest
import yaml

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'{name}.py')
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


@pytest.fixture(scope='module')
def router():
    litellm = pytest.importorskip('litellm')
    return litellm.Router(model_list=yaml.safe_load((ROOT / 'config.yaml').read_text())['model_list'])


def response_and_cost(deployment, stream=False):
    import litellm
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    from litellm.proxy.pass_through_endpoints.llm_provider_handlers.anthropic_passthrough_logging_handler import AnthropicPassthroughLoggingHandler
    usage = AnthropicConfig().calculate_usage({
        'input_tokens': 100, 'output_tokens': 50,
        'cache_read_input_tokens': 200, 'cache_creation_input_tokens': 1000,
        'cache_creation': {'ephemeral_5m_input_tokens': 600, 'ephemeral_1h_input_tokens': 400},
    }, reasoning_content=None)
    result = litellm.ModelResponse(model=deployment['litellm_params']['model'], usage=usage)
    # The native /v1/messages handler uses this deployment ID to pick prices.
    logging = SimpleNamespace(
        model_call_details={'custom_llm_provider': 'anthropic', 'stream': stream},
        litellm_params={}, litellm_call_id='synthetic-accounting-test',
        get_router_model_id=lambda: deployment['model_info']['id'],
    )
    kwargs = AnthropicPassthroughLoggingHandler._create_anthropic_response_logging_payload(
        result, deployment['litellm_params']['model'].split('/', 1)[1], {},
        datetime.now(), datetime.now(), logging,
    )
    assert logging.model_call_details['response_cost'] == kwargs['response_cost']
    return result, kwargs['response_cost']


@pytest.mark.parametrize('stream', [False, True])
def test_every_alias_native_anthropic_cache_accounting(router, stream):
    for deployment in router.model_list:
        _, cost = response_and_cost(deployment, stream)
        # 100 uncached + 200 cache hits + 600 5m writes + 400 1h writes + 50 output.
        expected = .00384 if 'sonnet' in deployment['model_name'] else .00192
        assert cost == pytest.approx(expected)


def test_dm_and_group_costs_reach_one_team_and_cut_off(router, monkeypatch):
    import litellm
    import litellm.proxy.proxy_server as proxy
    from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
    from litellm.proxy.auth.auth_checks import _team_max_budget_check
    from litellm.proxy._types import LiteLLM_TeamTable
    result, cost = response_and_cost(router.model_list[1])
    writer = AsyncMock()
    counter = AsyncMock()
    monkeypatch.setattr(proxy, 'proxy_logging_obj', SimpleNamespace(db_spend_update_writer=SimpleNamespace(update_database=writer), failed_tracking_alert=AsyncMock(), slack_alerting_instance=SimpleNamespace(customer_spend_alert=AsyncMock())))
    monkeypatch.setattr(proxy, 'increment_spend_counters', counter)
    monkeypatch.setattr(proxy, 'update_cache', AsyncMock())
    async def run():
        for key in ['sk-dm-only', 'sk-group-only']:
            await _ProxyDBLogger().async_log_success_event({
                'response_cost': cost, 'call_type': 'anthropic_messages',
                'litellm_params': {'metadata': {
                    'user_api_key': key, 'user_api_key_user_id': 'owner',
                    'user_api_key_team_id': 'sponsor-team',
                }},
            }, result, datetime.now(), datetime.now())
        assert proxy.proxy_logging_obj.failed_tracking_alert.await_count == 0, proxy.proxy_logging_obj.failed_tracking_alert.call_args_list
        assert writer.await_count == 2
        assert counter.await_count == 2
        assert all(c.kwargs['team_id'] == 'sponsor-team' for c in writer.await_args_list)
        spend = 9.999 + sum(c.kwargs['response_cost'] for c in writer.await_args_list)
        monkeypatch.setattr(proxy, 'get_current_spend', AsyncMock(return_value=spend))
        with pytest.raises(litellm.BudgetExceededError):
            await _team_max_budget_check(LiteLLM_TeamTable(team_id='sponsor-team',max_budget=10,spend=spend), None, proxy.proxy_logging_obj)
    asyncio.run(run())


def test_output_and_thinking_are_bounded():
    pytest.importorskip('litellm')
    limits = module('pilot_limits').pilot_limits
    for call_type in ['completion', 'anthropic_messages', 'pass_through_endpoint']:
        data = {'model': 'claude-sonnet-5', 'messages': [], 'max_tokens': 64000, 'thinking': {'type':'enabled','budget_tokens':32000}}
        result = asyncio.run(limits.async_pre_call_hook(None,None,data,call_type))
        assert result['max_tokens'] == 8192
        assert result['thinking']['budget_tokens'] == 8191


def test_listener_serves_ipv4_and_ipv6():
    """Actual HTTP over both socket families through the production launcher."""
    import multiprocessing
    import socket
    import time
    pytest.importorskip('uvicorn')
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    async def app(scope, receive, send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'healthy'})
    proc = multiprocessing.get_context('fork').Process(target=module('serve').dualstack_run, args=(app,), kwargs={'port':port,'lifespan':'off','log_level':'error'})
    proc.start()
    try:
        for address in [('127.0.0.1',port),('::1',port)]:
            for attempt in range(100):
                try:
                    conn = socket.create_connection(address, timeout=.2)
                    break
                except OSError:
                    if attempt == 99: raise
                    time.sleep(.05)
            with conn:
                conn.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                data=b''
                while chunk:=conn.recv(4096): data+=chunk
                assert b'200 OK' in data and b'healthy' in data
    finally:
        proc.terminate()
        proc.join(10)
