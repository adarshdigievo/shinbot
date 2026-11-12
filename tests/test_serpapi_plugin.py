import importlib.util
import json
import logging
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "serpapi_fixture", ROOT / "src/shinbot/bundled/official/plugins/serpapi/server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("SHINBOT_PLUGIN_SERPAPI_KEY", "secret-test-key")
    return module


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )


def test_search_sends_one_bounded_request_without_leaking_credentials(plugin, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "serpapi.com"
        assert request.url.params["api_key"] == "secret-test-key"
        assert request.url.params["engine"] == "google_light"
        assert request.url.params["q"] == "Coffee in Kochi"
        return httpx.Response(
            200,
            json={
                "search_metadata": {
                    "status": "Success",
                    "id": "search1",
                    "json_endpoint": "https://example/?api_key=secret-test-key",
                },
                "search_parameters": {"api_key": "secret-test-key"},
                "organic_results": [
                    {"title": "Source", "link": "https://example.org", "snippet": "secret-test-key"}
                ]
                * 30,
            },
        )

    transport(monkeypatch, handler)
    result = plugin.web_search("Coffee in Kochi", limit=3, country="in")
    assert len(calls) == 1
    assert len(result["data"]["organic_results"]) == 3
    assert "secret-test-key" not in json.dumps(result)
    assert "secret-test-key" not in caplog.text
    assert result["data"]["search_metadata"]["id"] == "search1"
    for params in [{"api_key": "overwrite"}, {"no_cache": True}, {"async": True}, {"q": ["bad"]}]:
        with pytest.raises(ValueError):
            plugin.search("google_light", params)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_errors_do_not_expose_key_or_follow_redirects(plugin, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"location": "https://evil.example"}, json={"error": "secret-test-key"}
        )

    transport(monkeypatch, handler)
    with pytest.raises(ValueError) as exc:
        plugin.web_search("test")
    assert "secret-test-key" not in str(exc.value) and len(calls) == 1


def test_transcripts_preserve_timestamps_and_page_without_unbounded_output(plugin, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["v"] == "Gk8gB5VACZw"
        assert request.url.params["language_code"] == "en"
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "transcript1", "status": "Success"},
                "transcript": [
                    {"start_ms": i * 1000, "end_ms": i * 1000 + 999, "snippet": "x" * 4000}
                    for i in range(100)
                ],
                "available_transcripts": [{"language_code": "en", "selected": True}],
            },
        )

    transport(monkeypatch, handler)
    result = plugin.youtube_transcript("https://www.youtube.com/watch?v=Gk8gB5VACZw")
    assert len(json.dumps(result)) < 30000
    assert result["next_start_ms"] == 6000
    next_result = plugin.youtube_transcript("Gk8gB5VACZw", start_ms=result["next_start_ms"])
    assert next_result["segments"][0]["start_ms"] == 6000
    assert len(calls) == 2
    with pytest.raises(ValueError):
        plugin.youtube_transcript("https://youtube.com.evil.example/watch?v=Gk8gB5VACZw")
    assert len(calls) == 2


def test_api_error_and_network_exception_are_sanitized(plugin, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"error": "secret-test-key"}))
    with pytest.raises(ValueError, match="could not complete"):
        plugin.web_search("test")

    def failed(request):
        raise httpx.ConnectError("secret-test-key", request=request)

    # Use a direct request override to retain the original client constructor once.
    monkeypatch.undo()
    monkeypatch.setenv("SHINBOT_PLUGIN_SERPAPI_KEY", "secret-test-key")
    transport(monkeypatch, failed)
    with pytest.raises(ValueError) as exc:
        plugin.web_search("test")
    assert "secret-test-key" not in str(exc.value)
