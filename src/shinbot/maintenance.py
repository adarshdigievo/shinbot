"""Host-side recovery, independent of the gateway and provider connections."""

import asyncio
import contextlib
import copy
import fcntl
import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import typer
from packaging.version import Version

from . import __version__
from .volume_admin import VOLUMES, validate_archive

SERVICES = ("control", "runtime", "computer", "browser", "telegram")
CORE = SERVICES[:4]
_ACTIVE_LOCK = None


def launcher():
    from . import cli

    return cli


@contextlib.contextmanager
def locked():
    global _ACTIVE_LOCK
    cli = launcher()
    with (cli.home() / "maintenance.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter(
                "Another installation command is running. Let it finish first."
            ) from None
        previous = _ACTIVE_LOCK
        _ACTIVE_LOCK = lock.fileno()
        try:
            yield
        finally:
            _ACTIVE_LOCK = previous


def child_lock():
    # A Docker command may outlive a killed launcher. Keep its mutation serialized.
    return {"pass_fds": (_ACTIVE_LOCK,)} if _ACTIVE_LOCK is not None else {}


def guarded(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        with locked():
            check_pending()
            return function(*args, **kwargs)

    return run


def check_pending():
    state = launcher().home() / "recovery.json"
    if state.exists() and json.loads(state.read_text()).get("pending"):
        raise typer.BadParameter("An update or rollback was interrupted. Run shinbot rollback to recover.")


def write_json(path, value):
    launcher().write_private(path, json.dumps(value, indent=2) + "\n")


def compose_document(path, configuration):
    # Resolved Compose values are literal, including dollars in passwords and commands.
    launcher().write_private(path, json.dumps(configuration, indent=2).replace("$", "$$") + "\n")


def configuration(path=None):
    cli = launcher()
    result = cli.compose(
        "--profile", "telegram", "config", "--format", "json", capture=True, configuration=path
    )
    # Compose emits dollars escaped for reuse as another Compose input, even in JSON.
    # Keep the in-memory snapshot literal; compose_document escapes it exactly once.
    return json.loads(result.stdout.replace("$$", "$"))


def stop_writers():
    cli = launcher()
    available = configuration()["services"]
    cli.compose("--profile", "telegram", "stop", "-t", "30", *[s for s in SERVICES if s in available])


def live_release():
    cli = launcher()
    config = configuration()
    for service in SERVICES:
        if service not in config["services"]:
            continue
        result = cli.compose("--profile", "telegram", "ps", "-aq", service, capture=True)
        ids = result.stdout.split()
        if not ids:
            if service in CORE:
                raise typer.BadParameter("Start all four core services before saving a release checkpoint.")
            continue
        image = subprocess.run(
            [*cli.docker(), "inspect", "--format", "{{.Image}}", ids[0]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not image.startswith("sha256:"):
            raise typer.BadParameter("Docker did not return an immutable image ID.")
        config["services"][service]["image"] = image
        config["services"][service].pop("build", None)
        config["services"][service]["pull_policy"] = "never"
    return {"version": __version__, "configuration": config}


def remember_release():
    cli = launcher()
    release = live_release()
    previous = cli.home() / "release.json"
    if (cli.home() / "rollback-compose.json").exists() and previous.exists():
        release["version"] = json.loads(previous.read_text())["version"]
    write_json(previous, release)
    return release


def digest(file):
    with Path(file).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_backup(destination, release=None):
    """Call only while writers are stopped. Never include an authenticated CLI session."""
    cli = launcher()
    destination = Path(destination).resolve()
    if destination.exists():
        raise typer.BadParameter("Choose a new backup filename.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = release or json.loads((cli.home() / "release.json").read_text())
    with tempfile.TemporaryDirectory(prefix=".shinbot-backup-", dir=destination.parent) as tmp:
        root = Path(tmp)
        cli.volume_operation("backup", root / "volumes.tar.gz", configuration=release["configuration"])
        shutil.copyfile(cli.home() / "deploy.env", root / "deploy.env")
        write_json(root / "release.json", release)
        manifest = {
            "format": 2,
            "version": __version__,
            "created": time.time(),
            "files": {p.name: digest(p) for p in root.iterdir()},
        }
        write_json(root / "manifest.json", manifest)
        target = root / "complete.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for name in ("manifest.json", "deploy.env", "release.json", "volumes.tar.gz"):
                archive.add(root / name, arcname=name)
        target.chmod(0o600)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        target.replace(destination)
    return destination


@contextlib.contextmanager
def unpack(source):
    """Validate all bytes before stopping or overwriting an installation."""
    source = Path(source).resolve()
    with tempfile.TemporaryDirectory(prefix="shinbot-restore-") as tmp:
        root = Path(tmp)
        with tarfile.open(source, "r:gz") as archive:
            names = archive.getnames()
            if "manifest.json" not in names:
                members = validate_archive(archive)
                if {Path(m.name).parts[0] for m in members} != VOLUMES:
                    raise ValueError("Backup must contain all five data volumes.")
                yield source, None, {"format": 1}
                return
            allowed = {"manifest.json", "deploy.env", "release.json", "volumes.tar.gz"}
            if set(names) != allowed or len(names) != len(allowed):
                raise ValueError("Backup has unexpected or duplicate files.")
            for member in archive.getmembers():
                if not member.isfile() or (member.name != "volumes.tar.gz" and member.size > 2_000_000):
                    raise ValueError("Backup contains an invalid file.")
                with archive.extractfile(member) as src, (root / member.name).open("wb") as dest:
                    shutil.copyfileobj(src, dest)
                (root / member.name).chmod(0o600)
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("format") != 2 or set(manifest.get("files", {})) != allowed - {"manifest.json"}:
            raise ValueError("Unsupported backup format.")
        for name, expected in manifest["files"].items():
            if digest(root / name) != expected:
                raise ValueError(f"Backup checksum failed for {name}.")
        with tarfile.open(root / "volumes.tar.gz", "r:gz") as archive:
            members = validate_archive(archive)
            if {Path(m.name).parts[0] for m in members} != VOLUMES:
                raise ValueError("Backup must contain all five data volumes.")
        yield root / "volumes.tar.gz", root, manifest


def backup(destination):
    cli = launcher()
    with locked():
        check_pending()
        release = remember_release()
        running = cli.running_services()
        stop_writers()
        try:
            archive_backup(destination, release)
        finally:
            if running:
                cli.compose("--profile", "telegram", "start", *running)
    typer.echo(
        f"Saved {destination}. Includes settings, plugins, conversations, files and credentials. Keep it private."
    )


def restore(source):
    cli = launcher()
    with locked(), unpack(source) as (volumes, root, manifest):
        check_pending()
        release = remember_release()
        running = cli.running_services()
        previous = cli.home() / "backups" / f"before-restore-{time.time_ns()}.tar.gz"
        stop_writers()
        try:
            archive_backup(previous, release)
        except BaseException:
            if running:
                cli.compose("--profile", "telegram", "start", *running)
            raise
        try:
            cli.volume_operation("restore", volumes)
            if root:
                saved = dict(
                    line.split("=", 1)
                    for line in (root / "deploy.env").read_text().splitlines()
                    if "=" in line and not line.startswith("#")
                )
                # Keep this host's connection, ports and service identities. Restore connector settings.
                current = cli.deployment()
                for key in ("TELEGRAM_OWNER_ID", "TELEGRAM_BOT_TOKEN"):
                    if key in saved:
                        current[key] = saved[key]
                    else:
                        current.pop(key, None)
                cli.write_private(
                    cli.home() / "deploy.env", "\n".join(f"{k}={v}" for k, v in current.items()) + "\n"
                )
                config = release["configuration"]
                for name in ("control", "telegram"):
                    if name in config["services"]:
                        env = config["services"][name].setdefault("environment", {})
                        for key in ("TELEGRAM_OWNER_ID", "TELEGRAM_BOT_TOKEN"):
                            if key in env:
                                env[key] = current.get(key, "")
                compose_document(cli.home() / "rollback-compose.json", config)
            (cli.home() / "client.json").unlink(missing_ok=True)
            if running:
                cli.compose("--profile", "telegram", "up", "-d", "--no-build", "--pull", "never", *running)
            write_json(cli.home() / "release.json", release)
            (cli.home() / "recovery.json").unlink(missing_ok=True)
        except BaseException:
            typer.echo(f"Restore stopped. Services remain stopped. Previous state: {previous}", err=True)
            raise
    typer.echo(
        f"Restored. Previous state: {previous}\nSign in and inspect recovery in Settings. Schedules and old work are held."
    )


def healthy(timeout):
    """Probe every core service from inside the gateway; no model call or Internet required."""
    cli = launcher()
    script = """import os,json,urllib.request
for url,key in [('http://127.0.0.1:8765/health',None),('http://runtime:8081/v1/health','SHINBOT_SERVICE_TOKEN'),('http://computer:8082/health','SHINBOT_COMPUTER_TOKEN'),('http://browser:8083/health','SHINBOT_BROWSER_TOKEN')]:
 r=urllib.request.Request(url,headers={'Authorization':'Bearer '+os.environ[key]} if key else {})
 assert json.load(urllib.request.urlopen(r,timeout=3))['ok']
"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = cli.compose(
                "exec", "-T", "control", "python", "-c", script, capture=True, check=False, timeout=20
            )
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("The core services did not become healthy before the deadline.")
        time.sleep(1)


def recovery_state():
    path = launcher().home() / "recovery.json"
    if not path.is_file():
        raise typer.BadParameter("No update checkpoint exists. Use shinbot restore with a saved backup.")
    return json.loads(path.read_text())


def rollback_saved(state, timeout):
    cli = launcher()
    with unpack(state["backup"]) as (volumes, root, _):
        config = state["release"]["configuration"]
        # Validate every retained image before stopping the candidate.
        for name in state["running"]:
            image = config["services"][name]["image"]
            subprocess.run([*cli.docker(), "image", "inspect", image], check=True, capture_output=True)
        stop_writers()
        compose_document(cli.home() / "rollback-compose.json", config)
        cli.write_private(cli.home() / "deploy.env", (root / "deploy.env").read_text())
        cli.volume_operation("restore", volumes)
        (cli.home() / "client.json").unlink(missing_ok=True)
        if state["running"]:
            cli.compose(
                "--profile", "telegram", "up", "-d", "--no-build", "--pull", "never", *state["running"]
            )
        if set(CORE) <= set(state["running"]):
            healthy(timeout)
        write_json(cli.home() / "release.json", state["release"])
        state["pending"] = False
        state["rolled_back"] = True
        write_json(cli.home() / "recovery.json", state)


def update(build=False, timeout=120):
    cli = launcher()
    with locked():
        check_pending()
        recorded = cli.home() / "release.json"
        if not recorded.exists():
            raise typer.BadParameter(
                "Run shinbot start once before upgrading the launcher to record the running release."
            )
        release = json.loads(recorded.read_text())
        running = cli.running_services()
        if not set(CORE) <= set(running):
            raise typer.BadParameter(
                "Start all four core services before updating, or use shinbot rollback to recover."
            )
        candidate = configuration(cli.compose_file())
        # Download/build first. A failed download leaves the current services untouched.
        try:
            cli.compose(
                "--profile",
                "telegram",
                *(["build"] if build else ["pull", "--policy", "missing"]),
                *running,
                configuration=cli.compose_file(),
                capture=True,
            )
        except (typer.BadParameter, subprocess.SubprocessError, OSError) as exc:
            raise typer.BadParameter(
                "Could not prepare the release. Your running services are unchanged. Check Docker, network access and disk space, then retry shinbot update."
            ) from exc
        checkpoint = cli.home() / "backups" / f"before-update-{time.time_ns()}.tar.gz"
        # Retain exact old image IDs even when a new build replaces their tags.
        for name in running:
            subprocess.run(
                [
                    *cli.docker(),
                    "tag",
                    release["configuration"]["services"][name]["image"],
                    f"shinbot-rollback/{cli.deployment().get('SHINBOT_PROJECT', 'shinbot')}-{name}:{checkpoint.stem}",
                ],
                check=True,
                capture_output=True,
            )
        stop_writers()
        try:
            archive_backup(checkpoint, release)
        except BaseException:
            cli.compose("--profile", "telegram", "start", *running)
            raise
        state = {"pending": True, "backup": str(checkpoint), "release": release, "running": running}
        try:
            write_json(cli.home() / "recovery.json", state)
        except BaseException:
            cli.compose("--profile", "telegram", "start", *running)
            raise
        try:
            held = copy.deepcopy(candidate)
            held["services"]["control"]["environment"]["SHINBOT_SAFE_MODE"] = "1"
            compose_document(cli.home() / "rollback-compose.json", held)
            cli.compose("--profile", "telegram", "up", "-d", "--no-build", "--pull", "never", *running)
            healthy(timeout)
            compose_document(cli.home() / "rollback-compose.json", candidate)
            cli.compose("--profile", "telegram", "up", "-d", "--no-build", "--pull", "never", *running)
            healthy(timeout)
            result = live_release()
            write_json(cli.home() / "release.json", result)
            (cli.home() / "rollback-compose.json").unlink()
            state["pending"] = False
            write_json(cli.home() / "recovery.json", state)
        except Exception:
            typer.echo("Update failed. Restoring the previous release and its data.", err=True)
            try:
                rollback_saved(state, timeout)
            except Exception:
                typer.echo(
                    "Recovery is incomplete. Run shinbot rollback after resolving Docker or disk errors. "
                    f"Checkpoint: {checkpoint}",
                    err=True,
                )
                raise typer.Exit(1) from None
            typer.echo(
                "Previous release restored. Sign in and review Recovery in Settings before resuming work.",
                err=True,
            )
            raise typer.Exit(1) from None
        typer.echo(
            f"Update healthy. Restore point: {checkpoint}\nRun shinbot rollback to return to that release and its data."
        )


def launcher_upgrade(version):
    """Choose the installer belonging to this launcher, without editing project checkouts."""
    from importlib.metadata import distribution

    installed = distribution("shinbot")
    direct = json.loads(installed.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable") or direct.get("vcs_info"):
        raise typer.BadParameter(
            "This launcher runs from source. Update the checkout, then run uv run --frozen shinbot update --build."
        )
    uv = shutil.which("uv")
    prefix = Path(sys.prefix)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON"))
        or key in {"UV_NATIVE_TLS", "UV_SYSTEM_CERTS", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR"}
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    if (prefix / "uv-receipt.toml").is_file():
        if not uv:
            raise typer.BadParameter("This launcher is managed by uv. Install uv before updating it.")
        import tomllib

        receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text())
        entries = receipt.get("tool", {}).get("entrypoints", [])
        path = next((entry.get("install-path") for entry in entries if entry.get("name") == "shinbot"), None)
        if not path:
            raise typer.BadParameter(
                "Could not identify this uv tool installation. Run uv tool upgrade shinbot, then shinbot update."
            )
        environment["UV_TOOL_DIR"] = str(prefix.parent)
        environment["UV_TOOL_BIN_DIR"] = str(Path(path).parent)
        command = [
            uv,
            "tool",
            "install",
            "--force",
            "--python",
            f"{sys.version_info.major}.{sys.version_info.minor}",
            "--no-config",
            "--default-index",
            "https://pypi.org/simple",
            f"shinbot=={version}",
        ]
    elif sys.prefix != sys.base_prefix:
        command = (
            [
                uv,
                "pip",
                "install",
                "--python",
                sys.executable,
                "--no-config",
                "--default-index",
                "https://pypi.org/simple",
            ]
            if uv
            else [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--index-url",
                "https://pypi.org/simple",
            ]
        )
        command.append(f"shinbot=={version}")
    else:
        raise typer.BadParameter(
            "Automatic launcher upgrades support uv tools and virtual environments. Upgrade shinbot with the package manager used to install it, then run shinbot update."
        )
    return command, environment


def update_latest(timeout=120):
    from .updates import discover

    cli = launcher()
    with locked():
        check_pending()
        saved = cli.home() / "release.json"
        if not saved.is_file():
            raise typer.BadParameter("Run shinbot start once to record the running release before updating.")
        current = json.loads(saved.read_text())["version"]
        result = asyncio.run(discover(current))
        if result["status"] == "current":
            typer.echo(result["message"])
            return
        if result["status"] != "available":
            raise typer.BadParameter(result["message"])
        target = result["latest_version"]
        if Version(__version__) > Version(target):
            raise typer.BadParameter(
                "This launcher is newer than the stable release. No downgrade was applied. Run shinbot update to apply the installed launcher version."
            )
        if not set(CORE) <= set(cli.running_services()):
            raise typer.BadParameter(
                "Start all four core services before updating, or run shinbot rollback to recover."
            )
        if __version__ != target:
            command, environment = launcher_upgrade(target)
            typer.echo(
                f"Installing ShinBot {target}. Current services will keep running until the new images are ready."
            )
            try:
                subprocess.run(command, env=environment, check=True, **child_lock())
            except (OSError, subprocess.SubprocessError) as exc:
                raise typer.BadParameter(
                    "The launcher upgrade failed. Services were not stopped. Check the package-manager error, then retry."
                ) from exc
    if __version__ == target:
        update(timeout=timeout)
        return
    # Start a fresh interpreter so loaded modules cannot mix old and new package versions.
    script = "import sys; from shinbot import __version__; assert __version__ == sys.argv.pop(1), 'Launcher version mismatch'; from shinbot.cli import app; app()"
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, target, "update", "--timeout", str(timeout)], check=False
    )
    if result.returncode:
        raise typer.Exit(result.returncode)


def rollback(timeout=120):
    cli = launcher()
    with locked():
        state = recovery_state()
        if state.get("rolled_back") and not state["pending"]:
            typer.echo("This checkpoint has already been restored. No data changed.")
            return
        # Preserve any work done since a successful update before reverting it.
        if not state["pending"]:
            running = cli.running_services()
            stop_writers()
            try:
                previous = cli.home() / "backups" / f"before-rollback-{time.time_ns()}.tar.gz"
                archive_backup(previous, json.loads((cli.home() / "release.json").read_text()))
                typer.echo(f"Saved current data: {previous}")
            except BaseException:
                if running:
                    cli.compose("--profile", "telegram", "start", *running)
                raise
        state["pending"] = True
        write_json(cli.home() / "recovery.json", state)
        rollback_saved(state, timeout)
    typer.echo(
        "Previous release and data restored. Sign in and inspect Recovery in Settings before resuming work."
    )
