"""Protect Pages project prefixes, cross-page anchors and search destinations."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_site", ROOT / "scripts/check_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


@pytest.fixture
def built_site(tmp_path):
    (tmp_path / ".nojekyll").touch()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/index.html").write_text('<h1 id="install">Install</h1>')
    (tmp_path / "docs/search.json").write_text(json.dumps([{"href": "index.html#install"}]))
    (tmp_path / "index.html").write_text('<a href="docs/index.html#install">Start</a>')
    return tmp_path


def test_project_relative_links_and_search_resolve(built_site):
    site.check(built_site)


@pytest.mark.parametrize(
    "link,reason",
    [
        ("/docs/", "outside project prefix"),
        ("docs/missing.html", "missing destination"),
        ("docs/index.html#missing", "missing anchor"),
    ],
)
def test_broken_navigation_fails_the_build(built_site, link, reason):
    (built_site / "index.html").write_text(f'<a href="{link}">Start</a>')
    with pytest.raises(ValueError, match=reason):
        site.check(built_site)


def test_missing_stylesheet_resource_fails_the_build(built_site):
    (built_site / "theme.css").write_text("body { background: url('missing.png'); }")
    with pytest.raises(ValueError, match="missing destination"):
        site.check(built_site)
