import json
from pathlib import Path

import pytest

from shinbot.extensions import skill_package
from shinbot.plugins import validate_package
from shinbot.sources import adapt_source, github_source, inspect_source

LICENSE = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
REV = "a" * 40
SKILL = b"---\nname: source-helper\ndescription: Organize useful research.\nlicense: MIT\n---\nRead references/tips.md.\n"
FILES = {
    "LICENSE": LICENSE,
    "NOTICE": b"Copyright (c) 2026 Original Authors",
    "skills/helper/SKILL.md": SKILL,
    "skills/helper/references/tips.md": b"Keep the sources.",
    ".claude/commands/research.md": b"Research $ARGUMENTS. Use Read and WebSearch.",
    "extensions/helper.ts": b"export default function(pi) {}",
    "package.json": b'{"license":"MIT","dependencies":{"unknown":"*"}}',
}


def test_inspect_and_adapt_immutable_sources_preserves_licenses_and_attribution():
    result = inspect_source("example/repo", REV, FILES)
    assert len(result["candidates"]) == 2 and all(x["license"] == "MIT" for x in result["candidates"])
    assert "package.json" in result["format_markers"]
    for path in ("skills/helper/SKILL.md", ".claude/commands/research.md"):
        files, origin = adapt_source("example/repo", REV, FILES, path)
        manifest, digest = validate_package(files)
        assert manifest.kind == "skill" and origin in manifest.source and digest
        assert files["LICENSE"] == LICENSE and files["NOTICE"] == b"Copyright (c) 2026 Original Authors"
        provenance = json.loads(files["SHINBOT-SOURCE.json"])
        assert provenance["revision"] == REV and provenance["path"] == path
        if path.endswith("SKILL.md"):
            assert files["SKILL.md"] == SKILL and files["references/tips.md"] == b"Keep the sources."
        else:
            assert files["references/original.md"] == FILES[path]
            assert b"does not install" in files["SKILL.md"]
    with pytest.raises(ValueError, match="needs an adapter"):
        adapt_source("example/repo", REV, FILES, "extensions/helper.ts")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r",
        "https://github.com.evil/o/r",
        "https://user:token@github.com/o/r",
        "https://github.com/o/r/blob/main/%2e%2e/a",
        "https://github.com/o/r?token=abc",
        "https://127.0.0.1/admin",
    ],
)
def test_source_rejects_unsafe_or_credentialed_urls(url):
    with pytest.raises(ValueError):
        github_source(url)


def test_skill_license_requires_real_notice_and_nearest_terms_win():
    with pytest.raises(ValueError, match="No upstream LICENSE"):
        skill_package({"SKILL.md": SKILL}, "MIT")
    with pytest.raises(ValueError, match="could not be verified"):
        skill_package({"SKILL.md": SKILL, "LICENSE": b"License declared by installer: MIT"}, "MIT")
    with pytest.raises(ValueError, match="does not match"):
        skill_package({"SKILL.md": SKILL, "LICENSE": LICENSE}, "Apache-2.0")
    files = {**FILES, "skills/helper/LICENSE": b"All rights reserved. No redistribution."}
    item = next(x for x in inspect_source("example/repo", REV, files)["candidates"] if x["format"] == "skill")
    assert item["license"] is None and item["error"]
    with pytest.raises(ValueError, match="could not be verified"):
        adapt_source("example/repo", REV, files, "skills/helper/SKILL.md")


async def test_source_import_pinning_scope_and_reserved_ids(application, owner, monkeypatch):
    core = application.state.core
    calls = []

    async def repository(repo, ref):
        calls.append((repo, ref))
        return REV, FILES

    monkeypatch.setattr(core.plugins, "repository", repository)
    result = owner.post("/api/plugins/source/inspect", json={"url": "https://github.com/example/repo"})
    assert result.status_code == 200
    assert (
        owner.post("/api/plugins/source/import", json={"url": "https://github.com/example/repo"}).status_code
        == 400
    )
    payload = {
        "url": result.json()["url"],
        "path": "skills/helper/SKILL.md",
        "access": {"mode": "bots", "bots": ["default"]},
    }
    installed = owner.post("/api/plugins/source/import", json=payload)
    assert installed.status_code == 200, installed.text
    item = installed.json()["installed"]
    assert item["status"] == "staged" and item["access"]["bots"] == ["default"]
    assert calls[-1] == ("example/repo", REV)
    assert REV in next(p for p in core.plugins.list() if p["id"] == item["id"])["source"]
    other = core.bots.save({"name": "Other"})["id"]
    with pytest.raises(ValueError, match="other bots"):
        await core.source_extension(payload, {"bot_id": other}, True)
    assert core.approval_required("plugins", {"operation": "import_source"}, {})


@pytest.mark.parametrize(
    "suffix",
    [b"\nCommons Clause: you may not sell this software.", b"\nThis software is for research use only."],
)
def test_standard_license_with_added_restrictions_is_not_approved(suffix):
    from shinbot.sources import detect_license

    with pytest.raises(ValueError):
        detect_license(LICENSE + suffix)


@pytest.mark.parametrize("path", ["skills/helper/EULA.md", "skills/helper/scripts/vendor/COPYING"])
def test_nested_or_nonstandard_license_requires_review(path):
    with pytest.raises(ValueError):
        adapt_source(
            "example/repo",
            REV,
            {**FILES, path: b"All rights reserved. No redistribution."},
            "skills/helper/SKILL.md",
        )


async def test_import_cannot_replace_unrelated_shared_skill(application, monkeypatch):
    core = application.state.core
    core.import_skills(
        [{"SKILL.md": SKILL.decode(), "LICENSE": LICENSE.decode()}],
        "MIT",
        "Local skill",
        {"mode": "shared", "bots": []},
    )

    async def repository(repo, ref):
        return REV, FILES

    monkeypatch.setattr(core.plugins, "repository", repository)
    with pytest.raises(ValueError, match="different extension"):
        await core.source_extension(
            {"url": f"https://github.com/example/repo/tree/{REV}", "path": "skills/helper/SKILL.md"},
            {"bot_id": "default"},
            True,
        )
    with pytest.raises(ValueError, match="reserved"):
        skill_package(
            {"SKILL.md": SKILL.replace(b"source-helper", b"learned-forged"), "LICENSE": LICENSE}, "MIT"
        )
    with pytest.raises(ValueError, match="reserved"):
        skill_package({"SKILL.md": SKILL, "LICENSE": LICENSE, "LEARNING.json": b"{}"}, "MIT")


def test_native_import_preserves_notices_and_rejects_nested_terms():
    package = skill_package({"SKILL.md": SKILL, "LICENSE": LICENSE}, "MIT")
    files = {"plugins/helper/" + name: data for name, data in package.items()}
    path = "plugins/helper/shinbot-plugin.json"
    adapted, _ = adapt_source("example/repo", REV, files, path)
    assert adapted == package
    with pytest.raises(ValueError, match="NOTICE unchanged"):
        adapt_source(
            "example/repo",
            REV,
            {**files, "plugins/helper/NOTICE": b"Copyright (c) 2026 Original Authors"},
            path,
        )
    with pytest.raises(ValueError, match="could not be verified"):
        adapt_source(
            "example/repo", REV, {**files, "plugins/helper/vendor/EULA.md": b"No redistribution."}, path
        )


@pytest.mark.parametrize(
    "restriction",
    [
        b"Commercial use requires a paid agreement.",
        b"for personal use exclusively",
        b"evaluation copy; redistribution requires written consent",
    ],
)
def test_restrictions_in_attribution_or_notice_are_never_discarded(restriction):
    from shinbot.sources import detect_license

    line = b"Copyright (c) 2026 ACME Corp. " + restriction
    original_line = next(line for line in LICENSE.splitlines() if line.startswith(b"Copyright"))
    with pytest.raises(ValueError):
        detect_license(LICENSE.replace(original_line, line))
    with pytest.raises(ValueError):
        adapt_source("example/repo", REV, {**FILES, "NOTICE": restriction}, "skills/helper/SKILL.md")
