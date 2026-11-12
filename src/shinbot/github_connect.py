"""Owner-driven GitHub CLI handoff and repository discovery."""

import asyncio
import hashlib
import hmac
import secrets
import shlex
import time

from . import integrations
from .github_inbox import ORG, REPO


class GitHubConnect:
    def __init__(self, core):
        self.core = core
        self.vault = core.plugins.vault
        self.pair = None
        self.connection_attempt = None
        self.lock = asyncio.Lock()

    def account(self):
        value = self.vault.read("github-account")
        return {
            "connected": bool(value.get("token")),
            "login": value.get("login", ""),
            "revision": value.get("revision", ""),
        }

    def start(self):
        self.connection_attempt = None
        code = secrets.token_urlsafe(32)
        self.pair = {
            "id": secrets.token_urlsafe(16),
            "digest": hashlib.sha256(code.encode()).hexdigest(),
            "expires": time.time() + 600,
            "status": "pending",
        }
        return {
            **self.progress(self.pair["id"]),
            "command": shlex.join([
                "shinbot", "github-connect", "--url", self.core.config.origin, "--code", code,
            ]),
        }

    def progress(self, pair_id):
        pair = self.pair
        if not pair or pair["id"] != pair_id:
            return {"id": pair_id, "status": "expired"}
        if pair["expires"] <= time.time() and pair["status"] == "pending":
            pair["status"] = "expired"
        return {key: value for key, value in pair.items() if key != "digest"}

    def cancel(self, pair_id):
        if self.pair and self.pair["id"] == pair_id and self.pair["status"] in {"pending", "connecting"}:
            self.pair["status"] = "cancelled"
            self.connection_attempt = None

    async def complete(self, code, token):
        async with self.lock:
            pair = self.pair
            if (
                not isinstance(code, str) or len(code) > 100 or not pair
                or pair["status"] != "pending" or pair["expires"] <= time.time()
                or not hmac.compare_digest(pair["digest"], hashlib.sha256(code.encode()).hexdigest())
            ):
                raise ValueError("This connection request expired. Start again in ShinBot.")
            pair["status"] = "connecting"
            try:
                result = await self.connect(token, pair=pair)
                pair.update(status="complete", login=result["login"])
                return result
            except ValueError:
                if pair["status"] == "connecting":
                    pair["status"] = "failed"
                raise

    async def connect(self, token, *, pair=None):
        if not isinstance(token, str) or not token.strip() or len(token) > 10000:
            raise ValueError("Choose a valid GitHub credential.")
        token = token.strip()
        if pair is None and self.pair:
            self.cancel(self.pair["id"])
        attempt = self.connection_attempt = object()
        async with integrations.client() as client:
            user, _ = await integrations.request_json(
                client, "GET", "https://api.github.com/user", headers=self.headers(token)
            )
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not ORG.fullmatch(login):
            raise ValueError("GitHub returned an invalid account identity.")
        if self.connection_attempt is not attempt or (pair and (
            self.pair is not pair or pair["status"] != "connecting" or pair["expires"] <= time.time()
        )):
            raise ValueError("This connection request was cancelled or replaced. Start again in ShinBot.")
        self.vault.update("github-account", {
            "token": token, "login": login, "revision": secrets.token_urlsafe(16),
        })
        return self.account()

    @staticmethod
    def headers(token):
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def discover(self, kind="repositories", page=1):
        if kind not in {"repositories", "organizations"} or not 1 <= page <= 100:
            raise ValueError("Choose a valid GitHub list and page.")
        account = self.vault.read("github-account")
        if not account.get("token"):
            raise ValueError("Connect your GitHub account first.")
        params = {"per_page": 100, "page": page}
        if kind == "repositories":
            params.update(sort="updated", direction="desc")
        async with integrations.client() as client:
            values, headers = await integrations.request_json(
                client, "GET", "https://api.github.com/user/" + ("repos" if kind == "repositories" else "orgs"),
                headers=self.headers(account["token"]), params=params,
            )
        if not isinstance(values, list):
            raise ValueError("GitHub returned an invalid list. Try again.")
        items = []
        for value in values:
            if not isinstance(value, dict):
                continue
            name = value.get("full_name" if kind == "repositories" else "login", "")
            if not isinstance(name, str) or not (REPO if kind == "repositories" else ORG).fullmatch(name):
                continue
            items.append({
                "name": name, "description": str(value.get("description") or "")[:200],
                "private": bool(value.get("private")),
            })
        return {
            "items": items, "page": page, "has_more": 'rel="next"' in headers.get("link", ""),
            "revision": account["revision"],
        }

    def select(self, payload):
        account = self.vault.read("github-account")
        if not account.get("token") or payload.get("revision") != account.get("revision"):
            raise ValueError("The GitHub account changed. Reload the repository list before saving.")
        repositories = payload.get("repositories", [])
        organizations = payload.get("organizations", [])
        if (
            not isinstance(repositories, list) or not isinstance(organizations, list)
            or len(repositories) > 25 or len(organizations) > 10
            or any(not isinstance(x, str) or not REPO.fullmatch(x) for x in repositories)
            or any(not isinstance(x, str) or not ORG.fullmatch(x) for x in organizations)
        ):
            raise ValueError("Choose valid repositories or organizations from the list.")
        if payload.get("target") == "inbox":
            return self.core.inbox.configuration({
                "repositories": repositories, "organizations": organizations, "token": account["token"],
            })
        if payload.get("target") != "plugin" or organizations or not repositories:
            raise ValueError("Choose at least one repository for the GitHub plugin.")
        version = payload.get("version")
        row, _ = self.core.plugins.package("github", version)
        if self.core.plugins.origin(row) != "adarshdigievo/shinbot-official-plugins":
            raise ValueError("This connection is for the official GitHub plugin. Configure other plugins separately.")
        self.core.plugins.configuration("github", version, {
            "SHINBOT_PLUGIN_GITHUB_TOKEN": account["token"],
            "SHINBOT_PLUGIN_GITHUB_REPOSITORIES": ",".join(repositories),
        })
        return {"ok": True}

    def disconnect(self):
        self.connection_attempt = None
        self.vault.update("github-account", {"token": None, "login": None, "revision": None})
        if self.pair:
            self.cancel(self.pair["id"])
        return self.account()
