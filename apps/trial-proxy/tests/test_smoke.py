"""Smoke harness failures must be prompt and must not leak child processes."""
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def smoke():
    path = Path(__file__).resolve().parents[1] / 'scripts/smoke.py'
    spec = importlib.util.spec_from_file_location('proxy_smoke_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readiness_fails_before_retrying_when_child_exited(smoke):
    child = SimpleNamespace(returncode=17, poll=lambda: 17)
    def unexpected():
        raise AssertionError('dead child must not be polled over HTTP')
    with pytest.raises(RuntimeError, match='status 17'):
        smoke.wait_for(unexpected, seconds=180, process=child)


def test_cleanup_of_exited_child_preserves_original_failure(smoke, monkeypatch):
    calls = []
    def gone(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(smoke.os, 'killpg', gone)
    child = SimpleNamespace(pid=123, wait=lambda timeout: calls.append(timeout))
    with pytest.raises(ValueError, match='original stage failure'):
        try:
            raise ValueError('original stage failure')
        finally:
            smoke.stop_process(child)
    assert calls == [20]


def test_cleanup_forces_and_reaps_unresponsive_child(smoke, monkeypatch):
    signals, waits = [], []
    monkeypatch.setattr(smoke.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    def wait(timeout=None):
        waits.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired('synthetic', timeout)
        return -9
    smoke.stop_process(SimpleNamespace(pid=123, wait=wait), timeout=.01)
    assert signals == [(123, smoke.signal.SIGTERM), (123, smoke.signal.SIGKILL)]
    assert waits == [.01, None]
