"""Release failures that would otherwise ship an unusable installer."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def checkout(tmp_path):
    for name in (
        "pyproject.toml",
        "uv.lock",
        "src/shinbot/__init__.py",
        "runtime/package.json",
        "runtime/package-lock.json",
        *release.COMPOSE_FILES,
    ):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    web = tmp_path / "src/shinbot/web"
    web.mkdir()
    (web / "index.html").write_text("<title>ShinBot</title>")
    return tmp_path


def test_release_identity_and_prerelease_must_agree(checkout):
    version, prerelease = release.check(checkout)
    assert release.check(checkout, "v" + version, prerelease) == (version, prerelease)
    with pytest.raises(ValueError, match="Release tag"):
        release.check(checkout, "v9.9.9", prerelease)
    with pytest.raises(ValueError, match="prerelease flag"):
        release.check(checkout, "v" + version, not prerelease)


@pytest.mark.parametrize(
    "filename", ["deploy/compose.release.yaml", "runtime/package.json", "src/shinbot/__init__.py"]
)
def test_mismatched_installer_components_block_release(checkout, filename):
    version, _ = release.check(checkout)
    path = checkout / filename
    old = release.runtime_version(version) if filename.endswith(".json") else version
    path.write_text(path.read_text().replace(old, "9.9.9"))
    with pytest.raises(ValueError):
        release.check(checkout)


def test_version_update_requires_a_fresh_lock_and_updates_every_image(checkout):
    old_version, _ = release.check(checkout)
    release.set_version("0.2.0rc2", checkout)
    with pytest.raises(ValueError, match="Refresh uv.lock"):
        release.check(checkout)
    lock = checkout / "uv.lock"
    lock.write_text(
        lock.read_text().replace(
            f'name = "shinbot"\nversion = "{old_version}"', 'name = "shinbot"\nversion = "0.2.0rc2"'
        )
    )
    assert release.check(checkout, "v0.2.0rc2", True) == ("0.2.0rc2", True)
    runtime = json.loads((checkout / "runtime/package-lock.json").read_text())
    assert runtime["packages"][""]["version"] == "0.2.0-rc.2"


@pytest.mark.parametrize("version", ["v1.2.3", "1.2", "01.2.3", "1.2.3+local", "1.2.3;echo"])
def test_invalid_version_does_not_edit_checkout(checkout, version):
    before = (checkout / "pyproject.toml").read_bytes()
    with pytest.raises(ValueError):
        release.set_version(version, checkout)
    assert (checkout / "pyproject.toml").read_bytes() == before


def test_wheel_verifies_packaged_ui_and_deployment(checkout):
    version, _ = release.check(checkout)
    distribution = checkout / "dist"
    distribution.mkdir()
    wheel = distribution / "shinbot.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"shinbot-{version}.dist-info/METADATA", f"Name: shinbot\nVersion: {version}\n")
        archive.writestr("shinbot/web/index.html", (checkout / "src/shinbot/web/index.html").read_bytes())
        archive.writestr(
            "shinbot/deployment/compose.yaml", (checkout / "deploy/compose.release.yaml").read_bytes()
        )
    release.check_wheel(distribution, checkout)
    (checkout / "src/shinbot/web/index.html").write_text("updated application")
    with pytest.raises(ValueError, match="Packaged web asset differs"):
        release.check_wheel(distribution, checkout)


def test_compose_fixture_uses_the_isolated_image_repository(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is not installed")
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
        pytest.skip("Docker Compose plugin is not installed")
    environment = tmp_path / "deploy.env"
    environment.write_text(
        "SHINBOT_IMAGE=shinbot-dev/shinbot\n"
        + "".join(
            f"SHINBOT_{service}_TOKEN=fixture-only-credential\n"
            for service in ("SERVICE", "COMPUTER", "BROWSER", "CHANNEL")
        )
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(environment),
            "--profile",
            "telegram",
            "-f",
            str(ROOT / "deploy/compose.yaml"),
            "-f",
            str(ROOT / "tests/compose.test.yaml"),
            "config",
            "--format",
            "json",
        ],
        env={k: v for k, v in os.environ.items() if not k.startswith("SHINBOT_")},
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert set(services) == {*release.SERVICES, "fixture"}
    assert all(value["image"].startswith("shinbot-dev/shinbot:") for value in services.values())
