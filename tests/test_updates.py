import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
import typer
from typer.testing import CliRunner

from shinbot import __version__, cli, maintenance, updates

discover_release = updates.discover


def release(version="1.10.0", **changes):
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, **changes}


def package(version="1.10.0", **changes):
    return {
        "info": {
            "name": "shinbot",
            "version": version,
            "project_urls": {"Repository": "https://github.com/adarshdigievo/shinbot"},
            "requires_python": ">=3.13",
        },
        "urls": [{"packagetype": "bdist_wheel", "yanked": False, "requires_python": ">=3.13"}],
        **changes,
    }


async def checked(github=None, pypi=None, current="1.9.0", extra=None):
    async def handler(request):
        data = (
            (release() if github is None else github)
            if str(request.url) == updates.GITHUB
            else (package() if pypi is None else pypi)
        )
        if extra is not None and str(request.url) not in {updates.GITHUB, updates.PYPI}:
            data = extra
        if isinstance(data, Exception):
            raise data
        if isinstance(data, int):
            return httpx.Response(data)
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await discover_release(current, client)


@pytest.mark.parametrize(
    "current,status",
    [("1.9.0", "available"), ("1.10.0", "current"), ("2.0.0", "ahead"), ("0.1.0a1", "available")],
)
async def test_release_comparison_uses_versions_and_never_downgrades(current, status):
    result = await checked(current=current)
    assert result["status"] == status
    assert result["latest_version"] == "1.10.0"
    assert result["release_url"].endswith("/tag/v1.10.0")


@pytest.mark.parametrize(
    "github,pypi,status",
    [
        (404, 404, "not_published"),
        (release(), 404, "not_published"),
        (403, package(), "unavailable"),
        (httpx.ConnectError("offline"), package(), "unavailable"),
        (release(prerelease=True), package(), "unavailable"),
        (release(draft=True), package(), "unavailable"),
        (release(tag_name="v1.0.0;touch /tmp/no"), package(), "unavailable"),
        (release(), package(urls=[]), "publishing"),
        (release(), package(urls=[{"packagetype": "bdist_wheel", "yanked": True}]), "publishing"),
        (
            release(),
            package(urls=[{"packagetype": "bdist_wheel", "yanked": False, "requires_python": ">=99"}]),
            "incompatible",
        ),
        (
            release(),
            {
                "info": {
                    "name": "shinbot",
                    "version": "1.10.0",
                    "project_urls": {"Repository": "https://github.com/another/shinbot"},
                }
            },
            "unavailable",
        ),
    ],
)
async def test_unpublished_offline_untrusted_and_incompatible_releases(github, pypi, status):
    result = await checked(github, pypi)
    assert result["status"] == status


async def test_pypi_prerelease_does_not_hide_the_matching_stable_package():
    result = await checked(pypi=package("2.0.0a1"), extra=package())
    assert result["status"] == "available"
    result = await checked(pypi=package("1.9.0"), extra=404)
    assert result["status"] == "publishing"


async def test_cached_check_coalesces_requests_and_persists_after_restart(application, monkeypatch):
    count = 0

    async def discover(**kwargs):
        nonlocal count
        count += 1
        await asyncio.sleep(0)
        return await checked(current=__version__)

    monkeypatch.setattr(updates, "discover", discover)
    checker = application.state.core.updates
    results = await asyncio.gather(*[checker.check(force=True) for _ in range(8)])
    assert count == 1
    assert all(r["status"] == "available" for r in results)
    assert updates.Updates(checker.db).snapshot()["latest_version"] == "1.10.0"
    checker.db.set("release_checks_enabled", False)
    assert updates.Updates(checker.db).snapshot()["automatic"] is False


def test_update_api_requires_owner_and_can_disable_checks(client, owner, application, monkeypatch):
    client.headers.pop("Authorization")
    assert client.get("/api/updates").status_code == 401
    client.headers["Authorization"] = "Bearer " + application.state.core.auth.session()
    assert owner.put("/api/updates/settings", json={"automatic": False}).json()["automatic"] is False
    assert owner.put("/api/updates/settings", json={"automatic": "false"}).status_code == 400
    assert owner.get("/api/updates").json()["status"] == "unchecked"


def test_check_command_does_not_invoke_installer(monkeypatch):
    async def discover():
        return await checked()

    monkeypatch.setattr(updates, "discover", discover)
    monkeypatch.setattr(maintenance, "update", lambda *a: pytest.fail("Must not update"))
    result = CliRunner().invoke(cli.app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert "1.10.0" in result.output and "--latest" in result.output


def test_launcher_installer_targets_current_venv_and_rejects_editable(monkeypatch, tmp_path):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda _: SimpleNamespace(read_text=lambda _: None)
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(maintenance.shutil, "which", lambda _: "/usr/bin/uv")
    command, _ = maintenance.launcher_upgrade("1.10.0")
    assert command[:5] == ["/usr/bin/uv", "pip", "install", "--python", sys.executable]
    assert command[-1] == "shinbot==1.10.0"
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda _: SimpleNamespace(read_text=lambda _: '{"dir_info":{"editable":true}}'),
    )
    with pytest.raises(typer.BadParameter, match="source"):
        maintenance.launcher_upgrade("1.10.0")


def test_uv_tool_upgrade_preserves_tool_and_binary_locations(monkeypatch, tmp_path):
    import importlib.metadata

    prefix = tmp_path / "tools/shinbot"
    prefix.mkdir(parents=True)
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{name="shinbot", install-path="/custom/bin/shinbot"}]\n'
    )
    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda _: SimpleNamespace(read_text=lambda _: None)
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(maintenance.shutil, "which", lambda _: "/usr/bin/uv")
    command, env = maintenance.launcher_upgrade("1.10.0")
    assert command[:4] == ["/usr/bin/uv", "tool", "install", "--force"]
    assert env["UV_TOOL_DIR"] == str(prefix.parent)
    assert env["UV_TOOL_BIN_DIR"] == "/custom/bin"


def test_latest_upgrade_hands_off_to_fresh_pinned_launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    (tmp_path / "release.json").write_text(json.dumps({"version": "1.9.0"}))

    async def discover(current):
        return await checked(current=current)

    monkeypatch.setattr(updates, "discover", discover)
    monkeypatch.setattr(cli, "running_services", lambda: maintenance.CORE)
    monkeypatch.setattr(maintenance, "launcher_upgrade", lambda v: (["fake-installer", v], {}))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(maintenance.subprocess, "run", run)
    maintenance.update_latest()
    assert calls[0] == ["fake-installer", "1.10.0"]
    assert calls[1][:3] == [sys.executable, "-I", "-c"]
    assert calls[1][4:] == ["1.10.0", "update", "--timeout", "120"]


def test_latest_upgrade_download_failure_leaves_services_running(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    (tmp_path / "release.json").write_text(json.dumps({"version": "1.9.0"}))

    async def discover(current):
        return await checked(current=current)

    monkeypatch.setattr(updates, "discover", discover)
    monkeypatch.setattr(cli, "running_services", lambda: maintenance.CORE)
    monkeypatch.setattr(maintenance, "launcher_upgrade", lambda v: (["fake-installer", v], {}))
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "install")),
    )
    monkeypatch.setattr(maintenance, "stop_writers", lambda: pytest.fail("Services must keep running"))
    with pytest.raises(typer.BadParameter, match="not stopped"):
        maintenance.update_latest()
    assert not (tmp_path / "recovery.json").exists()


async def test_offline_version_lookup_is_unavailable_not_publishing():
    result = await checked(pypi=package("2.0.0a1"), extra=httpx.ConnectError("offline"))
    assert result["status"] == "unavailable"
    assert result["sources"]["pypi"]["state"] == "unavailable"


async def test_failed_automatic_check_retries_after_one_hour(application, monkeypatch):
    now = 1000000
    monkeypatch.setattr(updates.time, "time", lambda: now)
    checker = application.state.core.updates
    count = 0

    async def discover(**kwargs):
        nonlocal count
        count += 1
        return await checked(github=503, current=__version__)

    monkeypatch.setattr(updates, "discover", discover)
    await checker.check()
    now += 3599
    await checker.check()
    assert count == 1
    now += 1
    await checker.check()
    assert count == 2


@pytest.mark.parametrize("uv", ["/usr/bin/uv", None])
def test_launcher_ignores_ambient_package_indexes(monkeypatch, tmp_path, uv):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda _: SimpleNamespace(read_text=lambda _: None)
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(maintenance.shutil, "which", lambda _: uv)
    for key in ["UV_INDEX", "UV_EXTRA_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_FIND_LINKS", "PYTHONPATH"]:
        monkeypatch.setenv(key, "/untrusted")
    command, environment = maintenance.launcher_upgrade("1.10.0")
    assert not any(
        key in environment
        for key in ["UV_INDEX", "UV_EXTRA_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_FIND_LINKS", "PYTHONPATH"]
    )
    assert environment["PIP_CONFIG_FILE"] == "/dev/null"
    assert "https://pypi.org/simple" in command
    if not uv:
        assert command[:4] == [sys.executable, "-I", "-m", "pip"] and "--isolated" in command


def test_matching_launcher_applies_images_without_reinstall_or_user_site_handoff(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    (tmp_path / "release.json").write_text(json.dumps({"version": "0.0.1"}))

    async def discover(current):
        return {"status": "available", "latest_version": __version__}

    monkeypatch.setattr(updates, "discover", discover)
    monkeypatch.setattr(cli, "running_services", lambda: maintenance.CORE)
    monkeypatch.setattr(maintenance, "launcher_upgrade", lambda v: pytest.fail("Already installed"))
    monkeypatch.setattr(maintenance.subprocess, "run", lambda *a, **kw: pytest.fail("No new interpreter"))
    calls = []
    monkeypatch.setattr(maintenance, "update", lambda **kw: calls.append(kw))
    maintenance.update_latest(timeout=180)
    assert calls == [{"timeout": 180}]
