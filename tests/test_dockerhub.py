"""Docker Hub maintenance must preserve release images and verified cache copies."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "dockerhub", Path(__file__).resolve().parents[1] / "scripts/dockerhub.py"
)
dockerhub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dockerhub)


class FakeHub:
    def __init__(self):
        self.calls = []
        self.overviews = {}
        self.deleted = set()

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "DELETE":
            self.deleted.add(path)
        elif method == "PATCH":
            self.overviews[path] = payload.copy()
        elif path in self.deleted:
            raise dockerhub.HubError(404, method, path)
        return self.overviews.get(path, {})


def cache_manifest(service):
    return {
        "config": {"mediaType": "application/vnd.buildkit.cacheconfig.v0", "digest": service},
        "layers": [{"digest": "layer-" + service}],
    }


def fake_registry(monkeypatch, corrupt=None):
    monkeypatch.setattr(dockerhub, "SERVICES", ("control", "browser"))
    images = {
        f"{dockerhub.REPOSITORY}:buildcache-{service}": cache_manifest(service)
        for service in dockerhub.SERVICES
    }

    def run(command, **kwargs):
        if command[3] == "inspect":
            return SimpleNamespace(stdout=json.dumps(images[command[-1]]))
        target, source = command[-2:]
        images[target] = images[source] if corrupt != target else cache_manifest("wrong")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dockerhub.subprocess, "run", run)
    return images


def test_migration_verifies_every_copy_before_deleting_sources(monkeypatch):
    images = fake_registry(monkeypatch)
    hub = FakeHub()
    original_request = hub.request

    def request(method, path, payload=None):
        if method == "DELETE":
            for service in dockerhub.SERVICES:
                assert images[f"{dockerhub.CACHE_REPOSITORY}:{service}"] == cache_manifest(service)
        return original_request(method, path, payload)

    hub.request = request
    dockerhub.migrate_caches(hub, delete_old=True)
    assert hub.deleted == {
        f"/v2/repositories/{dockerhub.REPOSITORY}/tags/buildcache-{service}/"
        for service in dockerhub.SERVICES
    }


def test_bad_copy_preserves_all_original_cache_tags(monkeypatch):
    fake_registry(monkeypatch, corrupt=f"{dockerhub.CACHE_REPOSITORY}:browser")
    hub = FakeHub()
    with pytest.raises(RuntimeError, match="changed while copying"):
        dockerhub.migrate_caches(hub, delete_old=True)
    assert not hub.deleted


def test_migration_never_treats_a_runnable_image_as_a_cache(monkeypatch):
    images = fake_registry(monkeypatch)
    images[f"{dockerhub.REPOSITORY}:buildcache-control"]["config"]["mediaType"] = (
        "application/vnd.oci.image.config.v1+json"
    )
    hub = FakeHub()
    with pytest.raises(ValueError, match="non-cache manifest"):
        dockerhub.migrate_caches(hub, delete_old=True)
    assert not hub.deleted
    assert not any(name.startswith(dockerhub.CACHE_REPOSITORY + ":") for name in images)


def test_copy_only_does_not_delete_source_tags(monkeypatch):
    fake_registry(monkeypatch)
    hub = FakeHub()
    dockerhub.migrate_caches(hub)
    assert not hub.deleted


def test_changed_source_blocks_cleanup(monkeypatch):
    images = fake_registry(monkeypatch)
    original_manifest = dockerhub.manifest
    reads = {}

    def manifest(reference):
        reads[reference] = reads.get(reference, 0) + 1
        if reference.endswith(":buildcache-control") and reads[reference] == 2:
            images[reference] = cache_manifest("newer-build")
        return original_manifest(reference)

    monkeypatch.setattr(dockerhub, "manifest", manifest)
    hub = FakeHub()
    with pytest.raises(RuntimeError, match="changed before cleanup"):
        dockerhub.migrate_caches(hub, delete_old=True)
    assert not hub.deleted


def test_overview_readback_detects_failed_update():
    hub = FakeHub()
    original_request = hub.request

    def request(method, path, payload=None):
        result = original_request(method, path, payload)
        if method == "GET" and path in hub.overviews:
            return {**result, "full_description": "stale overview"}
        return result

    hub.request = request
    with pytest.raises(RuntimeError, match="readback did not match"):
        dockerhub.publish_overviews(hub, "# ShinBot\nInstallation instructions.")


def test_empty_overview_does_not_change_the_registry():
    hub = FakeHub()
    with pytest.raises(ValueError, match="cannot be empty"):
        dockerhub.publish_overviews(hub, " \n")
    assert hub.calls == []


def test_http_error_does_not_disclose_credentials():
    secret = "test-only-private-token"

    class FailingOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 403, secret, {}, io.BytesIO(secret.encode())
            )

    hub = object.__new__(dockerhub.Hub)
    hub.token = secret
    hub.opener = FailingOpener()
    with pytest.raises(dockerhub.HubError) as failure:
        hub.request("PATCH", "/v2/repositories/adarshd96/shinbot/", {"description": "ShinBot"})
    assert failure.value.status == 403
    assert secret not in str(failure.value)
