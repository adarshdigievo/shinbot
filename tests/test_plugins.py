import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from shinbot.app import create_app
from shinbot.plugins import OFFICIAL, unpack_archive, validate_package


def skill(version="1.0.0"):
    manifest = {
        "schema_version": 1,
        "id": "test-skill",
        "name": "Test skill",
        "version": version,
        "description": "A test skill",
        "kind": "skill",
        "license": "MIT",
        "files": ["LICENSE", "SKILL.md"],
    }
    return {
        "shinbot-plugin.json": json.dumps(manifest).encode(),
        "LICENSE": b"MIT License\nPermission is hereby granted...",
        "SKILL.md": b"---\nname: test-skill\ndescription: A test skill\n---\nExplain the user's task.",
    }


def test_plugin_staging_immutable_versions_and_rollback(application):
    plugins = application.state.core.plugins
    defaults = plugins.versions()
    plugins.stage(skill())
    plugins.activate("test-skill", "1.0.0")
    altered = skill()
    altered["SKILL.md"] = b"Changed"
    with pytest.raises(ValueError, match="different contents"):
        plugins.stage(altered)
    plugins.stage(skill("1.0.1"))
    plugins.activate("test-skill", "1.0.1")
    assert plugins.versions() == [*defaults, "plugin:test-skill:1.0.1"]
    plugins.activate("test-skill", "1.0.0")
    assert plugins.versions() == [*defaults, "plugin:test-skill:1.0.0"]
    plugins.disable("test-skill")
    assert plugins.versions() == defaults


@pytest.mark.parametrize("altered", [False, True])
def test_plugin_install_recovers_files_left_before_database_commit(application, altered):
    plugins = application.state.core.plugins
    files = skill()
    path = plugins.directory / "test-skill" / "1.0.0"
    path.mkdir(parents=True)
    for name, content in files.items():
        (path / name).write_bytes(content)
    if altered:
        (path / "SKILL.md").write_bytes(b"Different package contents")
        with pytest.raises(ValueError, match="interrupted installation differ"):
            plugins.stage(files)
        assert (path / "SKILL.md").read_bytes() == b"Different package contents"
        assert not plugins.db.one("SELECT * FROM plugins WHERE id='test-skill'")
    else:
        installed = plugins.stage(files)
        assert installed["status"] == "staged"
        assert installed["digest"] == validate_package(files)[1]
        assert len(plugins.db.all("SELECT * FROM plugins WHERE id='test-skill'")) == 1
        plugins.activate("test-skill", "1.0.0")
        assert "plugin:test-skill:1.0.0" in plugins.versions()
    assert list(plugins.directory.glob("staging-*")) == []


def test_manifest_rejects_unreviewed_dependencies_and_traversal():
    files = skill()
    manifest = json.loads(files["shinbot-plugin.json"])
    manifest["requires"] = ["unreviewed-package"]
    files["shinbot-plugin.json"] = json.dumps(manifest).encode()
    with pytest.raises(ValueError, match="license audit"):
        validate_package(files)
    files = skill()
    files["../outside"] = b"escape"
    with pytest.raises(ValueError, match="traversal"):
        validate_package(files)


def test_marketplace_digest_and_install(application):
    files = skill()
    _, digest = validate_package(files)
    catalog = {
        "schema_version": 1,
        "name": "Personal",
        "plugins": [{"id": "test-skill", "version": "1.0.0", "path": "plugins/test", "sha256": digest}],
    }
    archive = {
        "shinbot-marketplace.json": json.dumps(catalog).encode(),
        **{f"plugins/test/{k}": v for k, v in files.items()},
    }
    plugins = application.state.core.plugins
    market = plugins.add_catalog("test/personal-plugins", "a" * 40, archive)
    staged = plugins.stage_from_catalog(market["id"], "test-skill", "1.0.0")
    assert staged["digest"] == digest and staged["status"] == "staged"
    archive["plugins/test/SKILL.md"] = b"Tampered"
    with pytest.raises(ValueError, match="digest mismatch"):
        plugins.add_catalog("test/personal-plugins", "b" * 40, archive)


def test_archive_rejects_escape():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("repo/../outside", "bad")
    with pytest.raises(ValueError, match="traversal"):
        unpack_archive(data.getvalue())


def test_official_marketplace_can_be_removed_and_readded_before_restart(application, monkeypatch):
    core = application.state.core
    files = skill()
    _, digest = validate_package(files)
    archive = {
        "shinbot-marketplace.json": json.dumps(
            {
                "schema_version": 1,
                "plugins": [
                    {"id": "test-skill", "version": "1.0.0", "path": "plugins/test", "sha256": digest}
                ],
            }
        ).encode(),
        **{f"plugins/test/{name}": content for name, content in files.items()},
    }

    async def repository(source, ref="HEAD"):
        return "a" * 40, archive

    monkeypatch.setattr(core.plugins, "repository", repository)
    headers = {"Authorization": "Bearer " + core.auth.session("cli")}
    with TestClient(application, headers=headers) as client:
        assert client.delete("/api/marketplaces/official").status_code == 200
        response = client.post("/api/marketplaces", json={"source": OFFICIAL})
        assert response.status_code == 200
        saved = response.json()

    restarted = create_app(core.config)
    with TestClient(restarted, headers=headers) as client:
        markets = client.get("/api/plugins").json()["marketplaces"]
        official = next(market for market in markets if market["source"] == OFFICIAL)
        assert official["id"] == saved["id"]
        assert official["revision"] == "a" * 40
        assert official["plugins"] == saved["plugins"]
        response = client.post(
            "/api/plugins/stage",
            json={"marketplace": official["id"], "id": "test-skill", "version": "1.0.0"},
        )
        assert response.status_code == 200
        assert response.json()["digest"] == digest
