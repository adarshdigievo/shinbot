"""Publish Docker Hub overviews and migrate the legacy build-cache tags."""

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "adarshd96/shinbot"
CACHE_REPOSITORY = "adarshd96/shinbot-buildcache"
SERVICES = ("control", "runtime", "computer", "browser", "desktop")
DESCRIPTION = "Self-hosted AI assistant with browser control, files, scheduled tasks, plugins, and bot profiles."
CACHE_DESCRIPTION = "BuildKit caches for ShinBot releases. Install ShinBot from adarshd96/shinbot."
CACHE_OVERVIEW = """# ShinBot build caches

BuildKit uses these caches to speed up ShinBot's release builds. Tags correspond to the control, runtime, computer, browser, and desktop build targets.

For runnable service images and installation instructions, visit [adarshd96/shinbot](https://hub.docker.com/r/adarshd96/shinbot).
"""


class HubError(RuntimeError):
    def __init__(self, status, method, path):
        self.status = status
        super().__init__(f"Docker Hub {method} {path} returned HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Hub:
    def __init__(self, username, secret):
        if not username or not secret:
            raise ValueError("Set DOCKERHUB_USERNAME and DOCKERHUB_TOKEN.")
        self.opener = urllib.request.build_opener(NoRedirect)
        self.token = None
        self.token = self.request(
            "POST", "/v2/auth/token", {"identifier": username, "secret": secret}
        )["access_token"]

    def request(self, method, path, payload=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            "https://hub.docker.com" + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            # Error bodies can contain request data; never print them or credentials.
            exc.close()
            raise HubError(exc.code, method, path) from None


def publish_overviews(hub, overview):
    if not overview.strip():
        raise ValueError("The Docker Hub overview cannot be empty.")
    cache_payload = {
        "name": CACHE_REPOSITORY.split("/")[1],
        "namespace": CACHE_REPOSITORY.split("/")[0],
        "registry": "docker.io",
        "is_private": False,
        "description": CACHE_DESCRIPTION,
        "full_description": CACHE_OVERVIEW,
    }
    try:
        hub.request("GET", f"/v2/namespaces/adarshd96/repositories/{cache_payload['name']}")
    except HubError as exc:
        if exc.status != 404:
            raise
        hub.request("POST", "/v2/namespaces/adarshd96/repositories", cache_payload)
    for repository, description, readme in (
        (REPOSITORY, DESCRIPTION, overview),
        (CACHE_REPOSITORY, CACHE_DESCRIPTION, CACHE_OVERVIEW),
    ):
        path = f"/v2/repositories/{repository}/"
        payload = {"description": description, "full_description": readme}
        hub.request("PATCH", path, payload)
        published = hub.request("GET", path)
        if any(published.get(key) != value for key, value in payload.items()):
            raise RuntimeError(f"Overview readback did not match for {repository}")
        print(f"Verified overview: {repository}", flush=True)


def manifest(reference):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def migrate_caches(hub, delete_old=False):
    copied = []
    for service in SERVICES:
        tag = "buildcache-" + service
        try:
            hub.request("GET", f"/v2/repositories/{REPOSITORY}/tags/{tag}/")
        except HubError as exc:
            if exc.status != 404:
                raise
            print(f"No legacy cache tag: {tag}", flush=True)
            continue
        source = f"{REPOSITORY}:{tag}"
        target = f"{CACHE_REPOSITORY}:{service}"
        original = manifest(source)
        if original.get("config", {}).get("mediaType") != "application/vnd.buildkit.cacheconfig.v0":
            raise ValueError(f"Refusing to migrate a non-cache manifest: {source}")
        subprocess.run(
            ["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", target, source],
            check=True,
        )
        if manifest(target) != original:
            raise RuntimeError(f"Cache manifest changed while copying {source}")
        copied.append((tag, original))
        print(f"Verified cache copy: {target}", flush=True)
    if delete_old:
        # Copy and verify the complete set before removing any source tag.
        for tag, original in copied:
            if manifest(f"{REPOSITORY}:{tag}") != original:
                raise RuntimeError(f"Source cache changed before cleanup: {tag}")
        for tag, _ in copied:
            hub.request("DELETE", f"/v2/repositories/{REPOSITORY}/tags/{tag}/")
            try:
                hub.request("GET", f"/v2/repositories/{REPOSITORY}/tags/{tag}/")
            except HubError as exc:
                if exc.status != 404:
                    raise
            else:
                raise RuntimeError(f"Legacy cache tag still exists: {tag}")
            print(f"Removed verified legacy cache tag: {tag}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-caches", action="store_true")
    parser.add_argument("--delete-old-cache-tags", action="store_true")
    args = parser.parse_args()
    if args.delete_old_cache_tags and not args.migrate_caches:
        parser.error("--delete-old-cache-tags requires --migrate-caches")
    overview = (ROOT / "deploy/DOCKERHUB.md").read_text()
    hub = Hub(os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN"))
    publish_overviews(hub, overview)
    if args.migrate_caches:
        migrate_caches(hub, delete_old=args.delete_old_cache_tags)


if __name__ == "__main__":
    main()
