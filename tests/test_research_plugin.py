import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from shinbot.plugins import validate_package

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "marketplaces/official/plugins/research"


@pytest.fixture
def plugin():
    spec = importlib.util.spec_from_file_location("research_test", PACKAGE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )


def date(days=0):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def test_partial_results_survive_blocks_and_rate_limits_without_credentials(plugin, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert "private-secret" not in str(request.url)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "42",
                            "title": "A story",
                            "created_at": date(),
                            "url": "https://example.org/article",
                        }
                    ]
                },
            )
        if request.url.host == "www.reddit.com":
            return httpx.Response(403, text="private-secret")
        return httpx.Response(429, headers={"Retry-After": "30"}, text="private-secret")

    transport(monkeypatch, handler)
    result = await plugin.search("agents", ["hacker_news", "reddit", "github_repositories"])
    assert [x["status"] for x in result["sources"]] == ["ok", "unavailable", "rate_limited"]
    assert result["sources"][0]["results"][0]["url"] == "https://news.ycombinator.com/item?id=42"
    assert result["sources"][2]["retry_after_seconds"] == 30
    assert "private-secret" not in json.dumps(result) and len(calls) == 3


@pytest.mark.parametrize(
    "source,query,payload",
    [
        (
            "reddit",
            "python",
            {
                "data": {
                    "children": [
                        {"data": {"id": "abc", "title": "Post", "created_utc": datetime.now(UTC).timestamp()}}
                    ]
                }
            },
        ),
        (
            "github_repositories",
            "python",
            {
                "items": [
                    {
                        "full_name": "a/b",
                        "html_url": "https://github.com/a/b",
                        "pushed_at": date(),
                        "description": "Project",
                    }
                ]
            },
        ),
        (
            "github_issues",
            "python",
            {
                "items": [
                    {
                        "title": "Bug",
                        "html_url": "https://github.com/a/b/issues/1",
                        "updated_at": date(),
                        "body": "Details",
                    }
                ]
            },
        ),
        ("devto", "python", [{"title": "Article", "url": "https://dev.to/a/b", "published_at": date()}]),
        ("polymarket", "bitcoin", {"events": [{"title": "Event", "slug": "event-1", "updatedAt": date()}]}),
        (
            "stocktwits",
            "$AAPL",
            {
                "messages": [
                    {
                        "id": 42,
                        "user": {"username": "author"},
                        "body": "$AAPL discussion",
                        "created_at": date(),
                    }
                ]
            },
        ),
        (
            "dripstack",
            "python",
            {
                "items": [
                    {
                        "title": "Newsletter",
                        "publicationSlug": "writer.substack.com",
                        "slug": "article",
                        "publishedAt": date(),
                    }
                ]
            },
        ),
    ],
)
async def test_source_adapters_return_citable_dates(plugin, monkeypatch, source, query, payload):
    calls = []

    def handler(request):
        calls.append(request)
        if source == "devto":
            assert request.url.path == "/api/articles" and request.url.params["tag"] == "python"
        if source == "stocktwits":
            assert request.url.path.endswith("/AAPL.json")
        if source.startswith("github"):
            assert ":>=" in request.url.params["q"]
        return httpx.Response(200, json=payload)

    transport(monkeypatch, handler)
    result = (await plugin.search(query, [source], limit=1))["sources"][0]
    assert result["status"] == "ok", result
    assert len(result["results"]) == 1 and len(calls) == 1
    item = result["results"][0]
    assert item["url"].startswith("https://") and item["date"].endswith("Z")
    assert item["date_basis"] == (
        "updated" if source.startswith("github") or source == "polymarket" else "published"
    )


async def test_date_filters_deduplication_and_output_bounds(plugin, monkeypatch):
    hits = [
        {"objectID": "1", "title": "Old", "created_at": date(90)},
        {"objectID": "2", "title": "Unknown", "created_at": None},
        {
            "objectID": "3",
            "title": "x" * 10000,
            "created_at": date(),
            "story_text": "<script>hidden</script><p>" + "y" * 10000 + "</p>",
            "points": {"untrusted": "z" * 10000},
        },
        {"objectID": "3", "title": "Duplicate", "created_at": date()},
        {"objectID": "4", "title": "Good", "created_at": date()},
    ]
    transport(monkeypatch, lambda request: httpx.Response(200, json={"hits": hits}))
    result = (await plugin.search("topic", ["hacker_news"], limit=3))["sources"][0]
    assert [x["thread_id"] for x in result["results"]] == ["3", "4"]
    assert result["excluded_old_or_undated"] == 2
    item = result["results"][0]
    assert len(item["title"]) <= 240 and len(item["snippet"]) <= 600 and "hidden" not in item["snippet"]
    assert "score" not in item
    all_time = (await plugin.search("topic", ["hacker_news"], days=0))["sources"][0]
    assert len(all_time["results"]) == 4 and not all_time["time_filter_applied"]


@pytest.mark.parametrize(
    "source,body",
    [
        (
            "arxiv",
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Agent paper</title><id>https://arxiv.org/abs/2610.12345</id><summary>Abstract</summary><published>DATE</published></entry></feed>',
        ),
        (
            "techmeme",
            "<rss><channel><item><title>Agent news</title><link>https://example.org/news</link><description>Agent story</description><pubDate>DATE</pubDate></item></channel></rss>",
        ),
    ],
)
async def test_atom_and_rss_search(plugin, monkeypatch, source, body):
    transport(monkeypatch, lambda request: httpx.Response(200, text=body.replace("DATE", date())))
    result = (await plugin.search("agent", [source]))["sources"][0]
    assert result["status"] == "ok" and result["results"][0]["date"]


@pytest.mark.parametrize(
    "source,body",
    [
        ("arxiv", '<!DOCTYPE foo [<!ENTITY x "unsafe">]><feed>&x;</feed>'),
        ("techmeme", "<html>error</html>"),
        ("arxiv", "<html>error</html>"),
        ("hacker_news", '{"error":"secret upstream data"}'),
        ("hacker_news", "<html>challenge</html>"),
        ("web", "<html>anubis_challenge</html>"),
        ("web", "<html>Unexpected markup</html>"),
    ],
)
async def test_malformed_responses_are_not_empty_successes(plugin, monkeypatch, source, body):
    transport(monkeypatch, lambda request: httpx.Response(200, text=body))
    result = (await plugin.search("agent", [source]))["sources"][0]
    assert result["status"] == "unavailable" and not result["results"]
    assert "secret upstream data" not in json.dumps(result)


async def test_web_results_decode_links_without_fetching_or_claiming_dates(plugin, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            text='<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Farticle">An article</a><a class="result__a" href="javascript:alert(1)">Bad link</a>',
        )

    transport(monkeypatch, handler)
    result = (await plugin.search("topic", ["web"], days=7))["sources"][0]
    assert len(result["results"]) == 1 and len(calls) == 1
    assert result["results"][0]["url"] == "https://example.org/article"
    assert result["results"][0]["date"] is None and not result["time_filter_applied"]


@pytest.mark.parametrize("status", [202, 301, 302, 401, 403, 500])
async def test_no_redirects_retries_or_challenge_bypass(plugin, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "http://127.0.0.1:8765/private"}, text="upstream-secret"
        )

    transport(monkeypatch, handler)
    result = await plugin.search("query", ["web"])
    assert result["sources"][0]["status"] == "unavailable" and len(calls) == 1
    assert "upstream-secret" not in json.dumps(result)


async def test_network_failure_and_response_limit(plugin, monkeypatch):
    def handler(request):
        if request.url.host == "www.reddit.com":
            raise httpx.ConnectError("private-network-details", request=request)
        return httpx.Response(200, content=b"x" * (plugin.MAX_BODY + 1))

    transport(monkeypatch, handler)
    result = await plugin.search("query", ["hacker_news", "reddit"])
    assert all(x["status"] == "unavailable" for x in result["sources"])
    assert "private-network-details" not in json.dumps(result)


async def test_source_deadline_preserves_other_results(plugin, monkeypatch):
    plugin.TIMEOUT = 0.03

    async def collect(source, *args):
        if source == "reddit":
            await asyncio.sleep(1)
        return [plugin.record("Story", "https://example.org", date=date())]

    monkeypatch.setattr(plugin, "collect", collect)
    result = await plugin.search("topic", ["hacker_news", "reddit"])
    assert [x["status"] for x in result["sources"]] == ["ok", "unavailable"]
    assert "timed out" in result["sources"][1]["error"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "q" * 401},
        {"query": "a\nb"},
        {"query": "q", "sources": []},
        {"query": "q", "sources": ["unknown"]},
        {"query": "q", "days": -1},
        {"query": "q", "limit": 11},
        {"query": "q", "sources": ["web", "reddit", "devto", "arxiv", "hacker_news"]},
        {"query": "two tags", "sources": ["devto"]},
        {"query": "../../secret", "sources": ["stocktwits"]},
    ],
)
async def test_invalid_inputs_do_not_make_requests(plugin, monkeypatch, arguments):
    transport(monkeypatch, lambda request: pytest.fail("Unexpected network request"))
    with pytest.raises(ValueError):
        await plugin.search(**arguments)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/x",
        "https://10.0.0.1",
        "https://localhost",
        "https://foo.local/x",
        "https://user:pass@example.com",
        "https://example.com:8765",
        "javascript:alert(1)",
        "https://example.org/\nfoo",
    ],
)
def test_unsafe_citations_are_rejected(plugin, url):
    assert plugin.safe_url(url) is None


async def test_arbitrary_fetch_targets_are_rejected(plugin, monkeypatch):
    transport(monkeypatch, lambda request: pytest.fail("Unexpected request"))
    for url in [
        "http://hn.algolia.com/api/v1/search",
        "https://hn.algolia.com.evil.test",
        "https://127.0.0.1/x",
        "https://user@api.github.com/repos/a/b",
    ]:
        with pytest.raises(ValueError):
            await plugin.request(url)
    for source, identifier in [
        ("web", "https://example.org"),
        ("github_issues", "a/..#1"),
        ("hacker_news", "1/../../secret"),
        ("reddit", "id?secret"),
    ]:
        with pytest.raises(ValueError):
            await plugin.read_thread(source, identifier)


async def test_discussion_excerpts_are_bounded_and_linked(plugin, monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Story",
                "created_at": date(),
                "children": [
                    {
                        "id": x,
                        "author": "person",
                        "text": "<p>A comment</p>",
                        "created_at": date(),
                        "children": [],
                    }
                    for x in range(10)
                ],
            },
        )

    transport(monkeypatch, handler)
    result = await plugin.read_thread("hacker_news", "42", 2)
    assert result["status"] == "ok" and len(result["comments"]) == 2
    assert result["comments"][0]["snippet"] == "A comment"


async def test_github_thread_reads_only_requested_public_issue_and_comments(plugin, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.github.com"
        if request.url.path.endswith("/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "body": "A reply",
                        "html_url": "https://github.com/a/b/issues/1#issuecomment-2",
                        "created_at": date(),
                        "user": {"login": "reader"},
                    }
                ],
            )
        assert request.url.path == "/repos/a/b/issues/1"
        return httpx.Response(
            200,
            json={
                "title": "Issue",
                "html_url": "https://github.com/a/b/issues/1",
                "comments": 1,
                "updated_at": date(),
            },
        )

    transport(monkeypatch, handler)
    result = await plugin.read_thread("github_issues", "a/b#1", 1)
    assert result["status"] == "ok" and len(calls) == 2 and len(result["comments"]) == 1


def test_package_mirror_digest_and_real_mcp_protocol(tmp_path):
    manifest = json.loads((PACKAGE / "shinbot-plugin.json").read_text())
    files = {name: (PACKAGE / name).read_bytes() for name in ["shinbot-plugin.json", *manifest["files"]]}
    validate_package(files)
    mirror = ROOT / "src/shinbot/bundled/official/plugins/research"
    assert all((mirror / name).read_bytes() == content for name, content in files.items())
    payload = {
        "operation": "mcp",
        "directory": str(PACKAGE),
        "manifest": manifest,
        "name": "sources",
        "arguments": {},
    }
    result = subprocess.run(
        [sys.executable, "-I", "-m", "shinbot.services.worker"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)["result"]
    assert not response["isError"] and "hacker_news" in json.dumps(response)


def test_research_is_catalog_only_on_fresh_install(application):
    core = application.state.core
    assert not core.db.one("SELECT id FROM plugins WHERE id='research'")
    assert any(p["id"] == "research" for m in core.plugins.catalog() for p in m["plugins"])


async def test_feed_keyword_matches_do_not_match_inside_unrelated_words(plugin, monkeypatch):
    body = "<rss><channel><item><title>A company said it is reopening</title><link>https://example.org/1</link><pubDate>DATE</pubDate></item><item><title>New AI model</title><link>https://example.org/2</link><pubDate>DATE</pubDate></item></channel></rss>".replace(
        "DATE", date()
    )
    transport(monkeypatch, lambda request: httpx.Response(200, text=body))
    result = (await plugin.search("AI", ["techmeme"]))["sources"][0]
    assert [x["url"] for x in result["results"]] == ["https://example.org/2"]
