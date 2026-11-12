"""GitHub tools for ShinBot. Credentials are supplied only to this plugin process."""

import base64
import json
import os
import re
from functools import wraps
from typing import Literal
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

mcp = FastMCP("GitHub")
API = "https://api.github.com"


class UncertainWrite(ValueError):
    pass


def reviewed_write(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except UncertainWrite as exc:
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text=str(exc))],
                _meta={"shinbot_outcome": "uncertain"},
            )

    return call


def repositories():
    values = [
        value.strip()
        for value in os.getenv("SHINBOT_PLUGIN_GITHUB_REPOSITORIES", "").split(",")
        if value.strip()
    ]
    if (
        not values
        or len(values) > 25
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
            or any(part in {".", ".."} for part in value.split("/"))
            for value in values
        )
    ):
        raise ValueError(
            "Configure GitHub with 1–25 repositories in owner/repository form, separated by commas."
        )
    return values


def repository(value):
    allowed = {name.lower(): name for name in repositories()}
    if value.lower() not in allowed:
        raise ValueError("This repository is not connected. Add it in Plugins → GitHub → Configure.")
    return "/repos/" + allowed[value.lower()]


def request(method, path, *, params=None, body=None):
    uncertain = UncertainWrite if method != "GET" else ValueError
    token = os.getenv("SHINBOT_PLUGIN_GITHUB_TOKEN", "").strip()
    if not token:
        raise ValueError("Set a GitHub token in Plugins → GitHub → Configure.")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "ShinBot-GitHub/1.0.0",
    }
    try:
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
            with client.stream(method, API + path, params=params, json=body, headers=headers) as response:
                if response.status_code in {403, 429} and (
                    response.headers.get("retry-after")
                    or response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    raise ValueError(
                        "GitHub's rate limit was reached. Wait before retrying; no automatic retry was made."
                    )
                errors = {
                    401: "GitHub rejected the token. Replace it in the plugin settings.",
                    403: "GitHub denied access. Check the token's repository permissions and organization approval.",
                    404: "GitHub could not find this item. Check its name and whether the token has access.",
                    409: "GitHub reported a conflict. Check the current repository state before trying again.",
                    422: "GitHub could not accept these inputs. Check the title, branch names and whether the item already exists.",
                }
                if response.status_code >= 300:
                    if response.status_code >= 500:
                        raise uncertain(
                            "GitHub did not confirm the result. Check GitHub before trying again; the change may have completed."
                        )
                    raise ValueError(
                        errors.get(
                            response.status_code,
                            f"GitHub returned HTTP {response.status_code}. Check GitHub before repeating a change.",
                        )
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise uncertain(
                            "GitHub's response is too large. Check GitHub before repeating a change."
                        )
                try:
                    return json.loads(content)
                except ValueError:
                    raise uncertain(
                        "GitHub returned an unreadable response. Check GitHub before repeating a change."
                    ) from None
    except httpx.HTTPError:
        raise uncertain(
            "The GitHub connection was interrupted. Check GitHub before repeating a change; it may have completed."
        ) from None


def bounded(text, limit=12000):
    text = text or ""
    return {"text": text[:limit], "truncated": len(text) > limit}


def item(data):
    return {
        key: data.get(key) for key in ("number", "title", "state", "html_url", "created_at", "updated_at")
    } | {
        "author": (data.get("user") or {}).get("login"),
        "body": bounded(data.get("body")),
        "is_pull_request": "pull_request" in data,
    }


def number_path(repo, kind, number):
    if number < 1:
        raise ValueError("Use a positive issue or pull-request number.")
    return f"{repository(repo)}/{kind}/{number}"


def pagination(page, per_page=20):
    if not 1 <= page <= 50:
        raise ValueError("Choose a page between 1 and 50.")
    return {"page": page, "per_page": per_page}


def title_text(title):
    if not title.strip() or len(title) > 256:
        raise ValueError("Use a title between 1 and 256 characters.")
    return title


def body_text(body):
    if len(body) > 60000:
        raise ValueError("Keep the message under 60,000 characters.")
    return body


@mcp.tool()
def connection_status() -> dict:
    """Check the connected GitHub account and configured repository list."""
    allowed = repositories()
    user = request("GET", "/user")
    return {"login": user["login"], "url": user["html_url"], "repositories": allowed}


@mcp.tool()
def get_repository(repo: str) -> dict:
    """Read a connected repository's description, default branch and permissions."""
    data = request("GET", repository(repo))
    return {
        key: data.get(key)
        for key in (
            "full_name",
            "description",
            "html_url",
            "private",
            "default_branch",
            "language",
            "open_issues_count",
            "permissions",
        )
    }


@mcp.tool()
def search_items(
    repo: str,
    text: str = "",
    kind: Literal["issue", "pr"] = "issue",
    state: Literal["open", "closed", "all"] = "open",
    page: int = 1,
) -> dict:
    """Search issues or pull requests in one connected repository. Text contains search words, not GitHub qualifiers."""
    repository(repo)
    if len(text) > 200 or any(c in text for c in ':"\\') or re.search(r"\b(OR|NOT|AND)\b", text):
        raise ValueError(
            "Use plain search words under 200 characters, without qualifiers or boolean operators."
        )
    query = f"repo:{repo} is:{kind} " + (f"state:{state} " if state != "all" else "") + text
    data = request("GET", "/search/issues", params={"q": query, "sort": "updated", **pagination(page)})
    return {
        "total": data["total_count"],
        "incomplete": data.get("incomplete_results", False),
        "page": page,
        "per_page": 20,
        "items": [
            item(row)
            for row in data["items"]
            if row.get("repository_url", "").lower() == (API + repository(repo)).lower()
        ],
    }


@mcp.tool()
def get_issue(repo: str, number: int, page: int = 1) -> dict:
    """Read an issue or pull-request conversation and a page of its comments."""
    path = number_path(repo, "issues", number)
    data = request("GET", path)
    comments = request("GET", path + "/comments", params=pagination(page))
    return {
        "item": item(data),
        "comments_page": page,
        "more_comments_possible": len(comments) == 20,
        "comments": [
            {
                "id": row["id"],
                "url": row["html_url"],
                "author": row["user"]["login"],
                "body": bounded(row.get("body"), 4000),
            }
            for row in comments
        ],
    }


@mcp.tool()
def get_pull_request(repo: str, number: int, page: int = 1) -> dict:
    """Read a pull request and one page of changed files, with bounded patches."""
    path = number_path(repo, "pulls", number)
    data = request("GET", path)
    files = request("GET", path + "/files", params=pagination(page))
    return {
        "item": item(data) | {"is_pull_request": True},
        "draft": data.get("draft"),
        "merged": data["merged"],
        "head": data["head"]["label"],
        "base": data["base"]["label"],
        "files_page": page,
        "more_files_possible": len(files) == 20,
        "files": [
            {
                "path": row["filename"],
                "status": row["status"],
                "additions": row["additions"],
                "deletions": row["deletions"],
                "patch": bounded(row.get("patch"), 3000),
            }
            for row in files
        ],
    }


@mcp.tool()
def read_file(repo: str, path: str, ref: str = "") -> dict:
    """Read a UTF-8 repository file or list a directory. Does not execute downloaded code."""
    if (
        len(path) > 1000
        or path.startswith("/")
        or "\\" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError("Use a relative repository path without dot or parent segments.")
    data = request(
        "GET", repository(repo) + "/contents/" + quote(path, safe="/"), params={"ref": ref} if ref else None
    )
    if isinstance(data, list):
        return {
            "entries": [
                {key: row.get(key) for key in ("name", "path", "type", "size")} for row in data[:100]
            ],
            "truncated": len(data) > 100,
        }
    if data.get("type") != "file" or data.get("encoding") != "base64" or data.get("size", 0) > 100000:
        raise ValueError("Choose a regular text file under 100 KB.")
    try:
        text = base64.b64decode(data["content"]).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("This file is not readable UTF-8 text.") from None
    return {
        "path": data["path"],
        "sha": data["sha"],
        "url": data["html_url"],
        "content": bounded(text, 30000),
    }


@mcp.tool()
@reviewed_write
def create_issue(repo: str, title: str, body: str = "") -> dict:
    """Create an issue after the owner approves its repository, title and body."""
    return item(
        request(
            "POST", repository(repo) + "/issues", body={"title": title_text(title), "body": body_text(body)}
        )
    )


@mcp.tool()
@reviewed_write
def comment(repo: str, number: int, body: str) -> dict:
    """Post an issue or pull-request comment after the owner approves the text."""
    if not body.strip():
        raise ValueError("Write a comment before sending it.")
    result = request(
        "POST", number_path(repo, "issues", number) + "/comments", body={"body": body_text(body)}
    )
    return {"id": result.get("id"), "url": result.get("html_url")}


@mcp.tool()
@reviewed_write
def update_issue(repo: str, number: int, state: Literal["open", "closed"]) -> dict:
    """Close or reopen an issue after owner approval. Pull requests use a different workflow."""
    path = number_path(repo, "issues", number)
    if "pull_request" in request("GET", path):
        raise ValueError("This is a pull request. This tool only changes issue state.")
    return item(request("PATCH", path, body={"state": state}))


@mcp.tool()
@reviewed_write
def create_pull_request(
    repo: str, title: str, head: str, base: str, body: str = "", draft: bool = True
) -> dict:
    """Create a pull request from existing branches after approval. Does not push commits or merge."""
    if not head.strip() or not base.strip() or max(len(head), len(base)) > 256:
        raise ValueError("Provide existing head and base branch names under 256 characters.")
    data = request(
        "POST",
        repository(repo) + "/pulls",
        body={
            "title": title_text(title),
            "head": head,
            "base": base,
            "body": body_text(body),
            "draft": draft,
        },
    )
    return item(data) | {"is_pull_request": True, "draft": data.get("draft")}


if __name__ == "__main__":
    mcp.run()
