import httpx
import pytest


@pytest.mark.parametrize(
    "upstream_status,body,detail",
    [
        (409, b'{"detail":"The browser is busy."}', "The browser is busy."),
        (503, b"<html>private upstream diagnostic</html>", "A service rejected the request."),
        (502, b'["private upstream diagnostic"]', "A service rejected the request."),
    ],
)
def test_browser_errors_preserve_service_status_without_exposing_raw_body(
    owner, application, monkeypatch, upstream_status, body, detail
):
    async def unavailable(*args, **kwargs):
        response = httpx.Response(
            upstream_status,
            content=body,
            request=httpx.Request("GET", "http://browser:8083/state"),
        )
        response.raise_for_status()

    monkeypatch.setattr(application.state.core, "remote", unavailable)
    response = owner.get("/api/browser")
    assert response.status_code == 502
    assert response.json() == {"detail": detail, "upstream_status": upstream_status}
