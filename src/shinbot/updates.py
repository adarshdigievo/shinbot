"""Read public release metadata; installation remains a host-side operation."""

import asyncio
import os
import re
import sys
import time

import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import __version__

REPOSITORY = "adarshdigievo/shinbot"
GITHUB = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PYPI = "https://pypi.org/pypi/shinbot/json"
RELEASES = f"https://github.com/{REPOSITORY}/releases"
CHECK_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 60 * 60


def release_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", value):
        raise ValueError("The release uses an unsupported version format.")
    return Version(value.removeprefix("v"))


async def metadata(client, url):
    try:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            if response.status_code == 404:
                return {"state": "not_published"}, None
            response.raise_for_status()
            content = bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content) > 2_000_000:
                    raise ValueError("Release metadata exceeds the size limit.")
        import json

        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Invalid release metadata.")
        return {"state": "available"}, data
    except (httpx.HTTPError, ValueError):
        return {"state": "unavailable"}, None


async def discover(current=__version__, client=None, python_version="current"):
    if client is None:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as connection:
            return await discover(current, connection, python_version)
    github, pypi = await asyncio.gather(metadata(client, GITHUB), metadata(client, PYPI))
    report = {
        "current_version": current,
        "latest_version": None,
        "status": "unavailable",
        "checked_at": time.time(),
        "release_url": RELEASES,
        "update_command": "shinbot update --latest",
        "sources": {"github": github[0], "pypi": pypi[0]},
        "message": "Could not verify a release. Check your connection and try again. Your installation is unchanged.",
    }
    if any(source[0]["state"] == "not_published" for source in (github, pypi)):
        report.update(
            status="not_published", message="A release is not available from both GitHub and PyPI yet."
        )
        return report
    if github[1] is None or pypi[1] is None:
        return report
    try:
        release, package = github[1], pypi[1]
        target = release_version(release.get("tag_name"))
        if (
            release.get("draft") is not False
            or release.get("prerelease") is not False
            or target.is_prerelease
        ):
            raise ValueError("No stable release.")
        info = package.get("info", {})
        if str(info.get("name", "")).lower() != "shinbot":
            raise ValueError("Unexpected package identity.")
        urls = info.get("project_urls") or {}
        if not isinstance(urls, dict) or not any(
            isinstance(url, str)
            and url.rstrip("/").removesuffix(".git").lower() == f"https://github.com/{REPOSITORY}"
            for url in urls.values()
        ):
            raise ValueError("Package does not identify the official repository.")
        report["latest_version"] = str(target)
        report["release_url"] = RELEASES + "/tag/" + release["tag_name"]
        if info.get("version") != str(target):
            source, package = await metadata(client, f"https://pypi.org/pypi/shinbot/{target}/json")
            if package is None:
                report["sources"]["pypi"] = source
                if source["state"] == "not_published":
                    report.update(
                        status="publishing",
                        message="The GitHub release is available, but its matching Python package is not ready. Check again later.",
                    )
                else:
                    report.update(latest_version=None, release_url=RELEASES)
                return report
            info = package.get("info", {})
        if info.get("version") != str(target) or info.get("name", "").lower() != "shinbot":
            raise ValueError("Release and package versions differ.")
        wheels = [
            item
            for item in package.get("urls", [])
            if item.get("packagetype") == "bdist_wheel" and item.get("yanked") is False
        ]
        if not wheels:
            report.update(
                status="publishing", message="This release has no available Python wheel. Check again later."
            )
            return report
        python = ".".join(map(str, sys.version_info[:3])) if python_version == "current" else python_version
        report["requires_python"] = info.get("requires_python") or "See release notes"
        if python is not None and not any(
            SpecifierSet(item.get("requires_python") or info.get("requires_python") or "").contains(python)
            for item in wheels
        ):
            report.update(
                status="incompatible",
                message=f"ShinBot {target} requires a newer or different Python version. Upgrade Python before updating.",
            )
            return report
        if target > Version(current):
            report.update(
                status="available",
                message=f"ShinBot {target} is available. Run the update command on the host where ShinBot is installed.",
            )
        elif target == Version(current):
            report.update(status="current", message="You are running the latest stable release.")
        else:
            report.update(
                status="ahead",
                message="Your installed version is newer than the latest stable release. No downgrade is suggested.",
            )
    except (ValueError, TypeError, AttributeError, InvalidVersion, InvalidSpecifier):
        report.update(
            status="unavailable",
            latest_version=None,
            release_url=RELEASES,
            message="Release metadata could not be verified. Your installation is unchanged.",
        )
    return report


class Updates:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    def snapshot(self):
        value = self.db.setting("release_check", {})
        if value.get("current_version") != __version__:
            value = {}
        return {
            "current_version": __version__,
            "status": "unchecked",
            "checked_at": None,
            "latest_version": None,
            "release_url": RELEASES,
            "update_command": "shinbot update --latest",
            "message": "Check GitHub and PyPI for a new stable release.",
            **value,
            "installation": "native"
            if os.getenv("SHINBOT_INSTALLATION") == "native" or os.getenv("SHINBOT_NATIVE_EXECUTION") == "1"
            else "docker",
            **(
                {"update_command": None}
                if os.getenv("SHINBOT_INSTALLATION") == "native"
                or os.getenv("SHINBOT_NATIVE_EXECUTION") == "1"
                else {}
            ),
            "automatic": self.db.setting("release_checks_enabled", True),
            "stale": bool(value and time.time() - value["checked_at"] >= CHECK_INTERVAL),
        }

    async def check(self, force=False):
        async with self.lock:
            saved = self.snapshot()
            age = time.time() - (saved["checked_at"] or 0)
            interval = RETRY_INTERVAL if saved["status"] in {"unavailable", "publishing"} else CHECK_INTERVAL
            if saved["checked_at"] and age < (60 if force else interval):
                return saved
            value = await discover(python_version=None)
            self.db.set("release_check", value)
            return self.snapshot()

    async def run(self):
        while True:
            if self.db.setting("release_checks_enabled", True):
                await self.check()
            await asyncio.sleep(3600)
