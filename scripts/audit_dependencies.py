"""Collect registry license declarations without installing or executing packages."""

import argparse
import concurrent.futures
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def registry_json(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise
            error = exc
            exc.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = exc
        if attempt == 2:
            raise RuntimeError(f"Dependency audit could not fetch {url} after 3 attempts: {error}") from error
        delay = 2**attempt
        print(f"Retrying {url} in {delay}s after {error}", file=sys.stderr, flush=True)
        time.sleep(delay)


def python_package(package):
    name, version = package["name"], package["version"]
    data = registry_json(f"https://pypi.org/pypi/{name}/{version}/json")
    info = data["info"]
    return {
        "ecosystem": "pypi",
        "name": name,
        "version": version,
        "license": info.get("license_expression") or info.get("license") or "UNKNOWN",
        "classifiers": [c for c in info.get("classifiers", []) if c.startswith("License")],
        "vulnerabilities": data.get("vulnerabilities", []),
        "artifacts": [{"url": f["url"], "sha256": f["digests"]["sha256"]} for f in data["urls"]],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--fail-vulnerable",
        action="store_true",
        help="Fail when PyPI reports an active advisory for a locked Python package.",
    )
    args = parser.parse_args()
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] != "shinbot"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        inventory = list(pool.map(python_package, packages))
    for lockfile in ("runtime/package-lock.json", "computer/package-lock.json", "browser/package-lock.json"):
        npm = json.loads((ROOT / lockfile).read_text())
        for path, p in npm["packages"].items():
            if not path:
                continue
            inventory.append(
                {
                    "ecosystem": "npm",
                    "name": path.split("node_modules/")[-1],
                    "version": p["version"],
                    "license": p.get("license", "UNKNOWN"),
                    "url": p.get("resolved"),
                    "integrity": p.get("integrity"),
                }
            )
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(inventory, indent=2) + "\n")
    for p in inventory:
        license_text = p["license"]
        if len(license_text) > 100:
            license_text = license_text[:100] + "…"
        print(f"{p['ecosystem']:4} {p['name']:42} {p['version']:14} {license_text}")
    vulnerable = [
        p for p in inventory if any(not item.get("withdrawn") for item in p.get("vulnerabilities", []))
    ]
    if args.fail_vulnerable and vulnerable:
        names = ", ".join(f"{p['name']}=={p['version']}" for p in vulnerable)
        raise SystemExit("Active Python dependency advisories require review: " + names)


if __name__ == "__main__":
    main()
