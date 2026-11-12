import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "audit_dependencies", Path(__file__).resolve().parents[1] / "scripts/audit_dependencies.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(TimeoutError("TLS handshake timed out")),
        TimeoutError("response timed out"),
        urllib.error.HTTPError("https://pypi.org", 429, "rate limited", {}, None),
        urllib.error.HTTPError("https://pypi.org", 503, "unavailable", {}, None),
    ],
)
def test_registry_recovers_from_transient_failure(monkeypatch, error):
    responses = iter([error, io.BytesIO(b'{"vulnerabilities": []}')])
    sleeps = []

    def request(url, timeout):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(audit.urllib.request, "urlopen", request)
    monkeypatch.setattr(audit.time, "sleep", sleeps.append)
    assert audit.registry_json("https://pypi.org/pypi/example/1/json") == {"vulnerabilities": []}
    assert sleeps == [1]


def test_registry_outage_fails_after_bounded_retries(monkeypatch):
    attempts = []
    sleeps = []

    def unavailable(url, timeout):
        attempts.append(url)
        raise urllib.error.URLError("network unavailable")

    monkeypatch.setattr(audit.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(audit.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="example/1/json after 3 attempts"):
        audit.registry_json("https://pypi.org/pypi/example/1/json")
    assert len(attempts) == 3
    assert sleeps == [1, 2]


def test_registry_does_not_retry_missing_package(monkeypatch):
    def missing(url, timeout):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr(audit.urllib.request, "urlopen", missing)
    monkeypatch.setattr(audit.time, "sleep", lambda _: pytest.fail("A 404 must not be retried"))
    with pytest.raises(urllib.error.HTTPError) as error:
        audit.registry_json("https://pypi.org/pypi/missing/1/json")
    assert error.value.code == 404


def test_active_advisory_still_blocks_release(monkeypatch, tmp_path):
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "example"\nversion = "1.0"\n')
    for folder in ("runtime", "computer", "browser"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "package-lock.json").write_text('{"packages": {}}')
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(
        audit,
        "registry_json",
        lambda _: {"info": {"license_expression": "MIT"}, "urls": [],
                   "vulnerabilities": [{"id": "TEST-ADVISORY", "withdrawn": None}]},
    )
    output = tmp_path / "inventory.json"
    monkeypatch.setattr(sys, "argv", ["audit_dependencies.py", str(output), "--fail-vulnerable"])
    with pytest.raises(SystemExit, match="example==1.0"):
        audit.main()
    assert json.loads(output.read_text())[0]["vulnerabilities"][0]["id"] == "TEST-ADVISORY"
