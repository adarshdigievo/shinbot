"""Keep package, runtime and container versions aligned before publishing."""

import argparse
import json
import re
import tomllib
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("control", "runtime", "computer", "browser", "telegram")
COMPOSE_FILES = ("deploy/compose.yaml", "deploy/compose.release.yaml", "tests/compose.test.yaml")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*))?")


def runtime_version(version):
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError("Use MAJOR.MINOR.PATCH, optionally followed by aN, bN or rcN (for example 0.2.0a1).")
    major, minor, patch, stage, number = match.groups()
    base = f"{major}.{minor}.{patch}"
    if stage:
        label = {"a": "alpha", "b": "beta", "rc": "rc"}[stage]
        return f"{base}-{label}.{number}"
    return base


def check(root=ROOT, tag=None, prerelease=None):
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    node_version = runtime_version(version)
    if tag is not None and tag != "v" + version:
        raise ValueError(f"Release tag must be v{version}; got {tag!r}.")
    is_prerelease = bool(VERSION.fullmatch(version).group(4))
    if prerelease is not None and prerelease != is_prerelease:
        raise ValueError("GitHub's prerelease flag must agree with the package version.")
    init = (root / "src/shinbot/__init__.py").read_text()
    if f'__version__ = "{version}"' not in init:
        raise ValueError("src/shinbot/__init__.py does not match pyproject.toml.")
    for filename in ("runtime/package.json", "runtime/package-lock.json"):
        data = json.loads((root / filename).read_text())
        if data["version"] != node_version or data.get("packages", {"": data})[""]["version"] != node_version:
            raise ValueError(f"{filename} does not match {node_version}.")
    for filename in COMPOSE_FILES:
        text = (root / filename).read_text()
        expected = ("control",) if filename.startswith("tests/") else SERVICES
        tags = re.findall(r"(?m)^\s*image: .*:(\S+)$", text)
        if sorted(tags) != sorted(f"{version}-{service}" for service in expected):
            raise ValueError(f"{filename} must reference the {version} service image set.")
    lock = tomllib.loads((root / "uv.lock").read_text())
    if next(p["version"] for p in lock["package"] if p["name"] == "shinbot") != version:
        raise ValueError("Refresh uv.lock with uv lock before releasing.")
    return version, is_prerelease


def set_version(version, root=ROOT):
    node_version = runtime_version(version)
    for filename, pattern, replacement in (
        ("pyproject.toml", r'(?m)^version = "[^"]+"', f'version = "{version}"'),
        ("src/shinbot/__init__.py", r'(?m)^__version__ = "[^"]+"', f'__version__ = "{version}"'),
    ):
        path = root / filename
        text, count = re.subn(pattern, replacement, path.read_text(), count=1)
        if count != 1:
            raise ValueError(f"Could not find version in {filename}.")
        path.write_text(text)
    for filename in ("runtime/package.json", "runtime/package-lock.json"):
        path = root / filename
        data = json.loads(path.read_text())
        data["version"] = node_version
        if "packages" in data:
            data["packages"][""]["version"] = node_version
        path.write_text(json.dumps(data, indent=2) + "\n")
    for filename in COMPOSE_FILES:
        path = root / filename
        path.write_text(
            re.sub(
                r"(image: .*:)[^\s:]+-(control|runtime|computer|browser|telegram)",
                lambda m: f"{m[1]}{version}-{m[2]}",
                path.read_text(),
            )
        )
    print("Versions updated. Run uv lock, then python scripts/release.py check.")


def check_wheel(directory, root=ROOT):
    version, _ = check(root)
    wheels = list(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Build into a clean directory containing exactly one wheel.")
    with ZipFile(wheels[0]) as wheel:
        metadata = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        info = BytesParser().parsebytes(wheel.read(metadata[0]))
        if info["Name"] != "shinbot" or info["Version"] != version:
            raise ValueError("Wheel metadata does not match this release.")
        for path in (root / "src/shinbot/web").rglob("*"):
            if (
                path.is_file()
                and wheel.read("shinbot/web/" + str(path.relative_to(root / "src/shinbot/web")))
                != path.read_bytes()
            ):
                raise ValueError(f"Packaged web asset differs: {path}")
        if (
            wheel.read("shinbot/deployment/compose.yaml")
            != (root / "deploy/compose.release.yaml").read_bytes()
        ):
            raise ValueError("Packaged Compose file does not match the release service images.")
        if any(name.startswith(("tmp/", "docs/", "site/", "experiments/")) for name in wheel.namelist()):
            raise ValueError("Internal or website files leaked into the application wheel.")
    print(f"Verified shinbot {version}: wheel metadata, web assets and Compose images.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("check")
    validate.add_argument("--tag")
    validate.add_argument("--prerelease", choices=("true", "false"))
    commands.add_parser("set-version").add_argument("version")
    commands.add_parser("check-wheel").add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "set-version":
            set_version(args.version)
        elif args.command == "check-wheel":
            check_wheel(args.directory)
        else:
            flag = None if args.prerelease is None else args.prerelease == "true"
            version, prerelease = check(tag=args.tag, prerelease=flag)
            print(f"version={version}\nstable={str(not prerelease).lower()}")
    except (ValueError, KeyError) as exc:
        parser.exit(1, f"Release check failed: {exc}\n")


if __name__ == "__main__":
    main()
