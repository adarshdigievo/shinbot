import pytest
from fastapi.testclient import TestClient

from shinbot.app import create_app
from shinbot.config import Config


@pytest.fixture
def application(tmp_path):
    application = create_app(
        Config(
            data=tmp_path,
            origin="http://testserver",
            coordinate=False,
            service_token="r" * 40,
            computer_token="c" * 40,
            browser_token="b" * 40,
            channel_token="t" * 40,
        )
    )

    core = application.state.core
    core.db.set(
        "preferences", {**core.db.setting("preferences"), "provider": "openai", "model": "test-model"}
    )
    return application


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture
def owner(client, application):
    result = client.post(
        "/auth/login",
        json={
            "bootstrap": application.state.core.auth.bootstrap,
            "password": "test-password-with-length",
            "cli": True,
        },
    )
    assert result.status_code == 200, result.text
    client.headers["Authorization"] = "Bearer " + result.json()["token"]
    return client
