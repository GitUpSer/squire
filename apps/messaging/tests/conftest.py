import os
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel
from messaging.config import settings
from messaging.db import engine, migrate
from messaging.main import app


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    url = os.environ.get("MESSAGING_TEST_DATABASE_URL", "sqlite:///" + str(tmp_path / "messages.db"))
    assert "_test" in url or url.startswith("sqlite"), "tests require a disposable test database"
    for key, value in {
        "DATABASE_URL": url, "ALLOW_SQLITE": "true", "ENCRYPTION_KEY": "e"*40,
        "HMAC_KEY": "h"*40, "CONTROL_TOKEN": "c"*40, "TRANSPORT_TOKEN": "t"*40,
        "OPERATOR_TOKEN": "o"*40, "CONTROL_URL": "https://control.test",
        "TRANSPORT_URL": "https://transport.test", "CONTROL_CALLBACK_TOKEN": "b"*40,
        "TRANSPORT_BROKER_TOKEN": "p"*40, "OUTBOUND_ENABLED": "true", "SEND_SPACING_SECONDS": "0",
    }.items():
        monkeypatch.setenv("MESSAGING_" + key, value)
    if engine.cache_info().currsize:
        engine().dispose()
    engine.cache_clear()
    settings.cache_clear()
    SQLModel.metadata.drop_all(engine())
    migrate()
    yield
    engine().dispose()
    engine.cache_clear()
    settings.cache_clear()


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def headers(role):
    return {"Authorization": "Bearer " + {"control": "c"*40, "transport": "t"*40,
        "operator": "o"*40, "dm": "d"*40, "group": "g"*40}[role]}
