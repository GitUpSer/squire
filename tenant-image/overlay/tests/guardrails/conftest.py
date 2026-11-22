"""Imported founder tests run against the isolated Squire runtime home."""
import pytest

@pytest.fixture(autouse=True)
def hermes_env(scope):
    from squire_overlay.guardrails import owned, policy
    policy.reset_for_tests()
    yield scope.home
    owned.reset_for_tests()

@pytest.fixture
def loaded_plugins(hermes):
    yield hermes
