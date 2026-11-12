"""Run a configured local installation as a macOS user service."""

import fcntl
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import typer

app = typer.Typer(help="Manage the native installation on this Mac.", no_args_is_help=True)
LABEL = "dev.shinbot.native"


def directory():
    return Path(os.getenv("SHINBOT_NATIVE_HOME", "~/.shinbot/native")).expanduser().resolve()


def configuration():
    root = directory()
    path = root / "config.json"
    if not path.is_file():
        raise ValueError("A native installation has not been configured on this Mac.")
    config = json.loads(path.read_text())
    if config.get("computer_consent") != "macos-account" and not config.get("desktop", {}).get("enabled"):
        raise ValueError("Native Computer needs the owner's explicit consent to use their macOS account.")
    for name in ("runtime", "computer", "browser", "channel"):
        if len(config["tokens"].get(name, "")) < 32:
            raise ValueError("The native service credentials are incomplete.")
    for name in ("python", "node"):
        if not Path(config[name]).is_file():
            raise ValueError(f"The configured {name} executable is missing.")
    if not (Path(config["runtime_dir"]) / "dist/server.js").is_file():
        raise ValueError("Build the Pi runtime before starting the native installation.")
    return root, config


def service_environment(root, config):
    # Credentials for services are provided separately from the Computer child environment.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SHINBOT_", "TELEGRAM_"))}
    ports, tokens = config["ports"], config["tokens"]
    env.update(
        SHINBOT_DATA=str(root / "control-data"),
        SHINBOT_RUNTIME_DATA=str(root / "runtime-data"),
        SHINBOT_BROWSER_DATA=str(root / "browser-data"),
        SHINBOT_EXECUTOR_DATA=str(root / "computer-data"),
        SHINBOT_WORKSPACE=str(root / "workspace"),
        SHINBOT_ORIGIN=f"http://localhost:{ports['control']}",
        SHINBOT_CONTROL_URL=f"http://127.0.0.1:{ports['control']}",
        SHINBOT_RUNTIME_URL=f"http://127.0.0.1:{ports['runtime']}",
        SHINBOT_COMPUTER_URL=f"http://127.0.0.1:{ports['computer']}",
        SHINBOT_BROWSER_URL=f"http://127.0.0.1:{ports['browser']}",
        SHINBOT_SERVICE_TOKEN=tokens["runtime"],
        SHINBOT_COMPUTER_TOKEN=tokens["computer"],
        SHINBOT_BROWSER_TOKEN=tokens["browser"],
        SHINBOT_CHANNEL_TOKEN=tokens["channel"],
        SHINBOT_NATIVE_EXECUTION="1",
        SHINBOT_INSTALLATION="native",
        SHINBOT_BROWSER_VIEW="native",
        SHINBOT_HEADLESS="1",
        SHINBOT_SAFE_MODE="1" if config.get("safe_mode", False) else "0",
        SHINBOT_CHILD_PATH=config["child_path"],
        SHINBOT_SKILLS_CLI=config["skills_cli"],
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
    )
    if config.get("desktop", {}).get("enabled"):
        env.update(SHINBOT_DESKTOP="1", SHINBOT_NATIVE_EXECUTION="0", SHINBOT_WORKSPACE="/workspace")
        from .desktop_host import saved_endpoints

        for name, url in saved_endpoints(root).items():
            env[f"SHINBOT_{name.upper()}_URL"] = url
    return env


def command(name, config):
    if name == "desktop":
        return [config["python"], "-m", "shinbot.desktop_host"]
    if name == "telegram":
        return [config["python"], "-m", "shinbot.services.telegram"]
    if name == "runtime":
        return [config["node"], str(Path(config["runtime_dir"]) / "dist/server.js")]
    module = "shinbot.app:create_app" if name == "control" else f"shinbot.services.{name}:app"
    return [
        config["python"],
        "-m",
        "uvicorn",
        module,
        "--host",
        "127.0.0.1",
        "--port",
        str(config["ports"][name]),
        "--no-access-log",
        *(["--factory"] if name == "control" else []),
    ]


def child_environment(name, root, config):
    env = service_environment(root, config)
    if name == "desktop":
        env["SHINBOT_NATIVE_HOME"] = str(root)
    if name == "runtime":
        env.update(PORT=str(config["ports"][name]), HOST="127.0.0.1")
    if name in {"computer", "browser"}:
        env["SHINBOT_EXECUTOR_TOKEN"] = config["tokens"][name]
    telegram = config.get("telegram", {})
    if telegram.get("enabled"):
        if name in {"control", "telegram"}:
            env["TELEGRAM_OWNER_ID"] = str(telegram["owner_id"])
        if name == "telegram":
            env["TELEGRAM_BOT_TOKEN"] = telegram["bot_token"]
    return env


def supervise():
    os.umask(0o077)
    root, config = configuration()
    lock = (root / "native.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    services = ["runtime", "computer", "browser", "control"]
    if config.get("desktop", {}).get("enabled"):
        services = ["runtime", "desktop", "control"]
    if config.get("telegram", {}).get("enabled"):
        services.append("telegram")
    logs = root / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    stopping = False
    children, streams, retries = {}, {}, {}
    desktop_urls = None

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            for name in services:
                if name == "control" and config.get("desktop", {}).get("enabled"):
                    from .desktop_host import saved_endpoints

                    current_urls = saved_endpoints(root)
                    if not current_urls:
                        continue
                    if (
                        desktop_urls
                        and current_urls != desktop_urls
                        and children.get(name)
                        and children[name].poll() is None
                    ):
                        children[name].terminate()
                    desktop_urls = current_urls
                previous = children.get(name)
                if previous and previous.poll() is None:
                    continue
                if time.monotonic() < retries.get(name, 0):
                    continue
                if name not in streams:
                    streams[name] = (logs / f"{name}.log").open("ab", buffering=0)
                if name == "desktop":
                    (root / "desktop-endpoints.json").unlink(missing_ok=True)
                children[name] = subprocess.Popen(
                    command(name, config),
                    env=child_environment(name, root, config),
                    cwd=root,
                    stdout=streams[name],
                    stderr=streams[name],
                    start_new_session=True,
                )
                retries[name] = time.monotonic() + 5
                (root / "processes.json").write_text(json.dumps({k: p.pid for k, p in children.items()}))
            time.sleep(0.5)
    finally:
        # Stop intake before the services that carry out its actions.
        for name in ("telegram", "control", "runtime", "computer", "browser", "desktop"):
            process = children.get(name)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=40 if name == "desktop" else 15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        for stream in streams.values():
            stream.close()
        (root / "processes.json").unlink(missing_ok=True)
        lock.close()


def service_target():
    if sys.platform != "darwin":
        raise ValueError("This native service manager currently supports macOS.")
    return f"gui/{os.getuid()}/{LABEL}"


@app.command()
def start():
    """Start native services and keep them running while signed into this Mac."""
    try:
        root, config = configuration()
        target = service_target()
        active = subprocess.run(["launchctl", "print", target], capture_output=True)
        if active.returncode == 0:
            typer.echo("Native ShinBot is already running.")
            return
        logs = root / "logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        values = {
            "Label": LABEL,
            "ProgramArguments": [config["python"], "-m", "shinbot.native"],
            "EnvironmentVariables": {"SHINBOT_NATIVE_HOME": str(root)},
            "WorkingDirectory": str(root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "ExitTimeOut": 90,
            "ProcessType": "Interactive",
            "StandardOutPath": str(logs / "supervisor.log"),
            "StandardErrorPath": str(logs / "supervisor.log"),
        }
        plist.write_bytes(plistlib.dumps(values))
        plist.chmod(0o600)
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True, capture_output=True
        )
        typer.echo(f"Native ShinBot started at http://localhost:{config['ports']['control']}/")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise typer.BadParameter(
            "Could not start native ShinBot. Check its configuration and native/logs."
        ) from error


@app.command()
def stop():
    """Stop native services and preserve all saved data."""
    target = service_target()
    if subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", target], check=True, capture_output=True)
    # A stopped installation must not silently restart at the next login.
    (Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist").unlink(missing_ok=True)
    # launchctl can return while graceful service shutdown is still in progress.
    with (directory() / "native.lock").open("a") as lock:
        deadline = time.monotonic() + 80
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise typer.BadParameter(
                        "Native services are still stopping. Check native/logs before restarting."
                    ) from None
                time.sleep(0.2)
    typer.echo("Native ShinBot stopped. Saved data is unchanged.")


@app.command()
def status():
    """Check native services without contacting Docker."""
    root, config = configuration()
    result = {"data": str(root), "services": {}}
    with httpx.Client(timeout=2, trust_env=False) as client:
        from .desktop_host import saved_endpoints

        desktop_urls = saved_endpoints(root) if config.get("desktop", {}).get("enabled") else {}
        for name, port in config["ports"].items():
            headers = {} if name == "control" else {"Authorization": "Bearer " + config["tokens"][name]}
            route = "/v1/health" if name == "runtime" else "/health"
            try:
                response = client.get(
                    desktop_urls.get(name, f"http://127.0.0.1:{port}") + route, headers=headers
                )
                result["services"][name] = "healthy" if response.is_success else "unavailable"
            except httpx.HTTPError:
                result["services"][name] = "stopped"
    if config.get("telegram", {}).get("enabled"):
        result["services"]["telegram"] = "stopped"
        try:
            pid = json.loads((root / "processes.json").read_text()).get("telegram")
            if pid:
                os.kill(pid, 0)
                result["services"]["telegram"] = "running"
        except (OSError, ValueError):
            pass
    typer.echo(json.dumps(result, indent=2))


def idle_for_backend_change(root, config):
    path = root / "control-data/shinbot.sqlite"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if (
            db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('running','queued','waiting') LIMIT 1"
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM actions WHERE status IN ('executing','approved','retrying') LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("Finish or pause active tasks before changing the computer backend.")
    try:
        from .desktop_host import saved_endpoints

        url = (
            saved_endpoints(root).get("browser", f"http://127.0.0.1:{config['ports']['browser']}")
            if config.get("desktop", {}).get("enabled")
            else f"http://127.0.0.1:{config['ports']['browser']}"
        )
        response = httpx.get(
            url + "/lease",
            headers={"Authorization": "Bearer " + config["tokens"]["browser"]},
            timeout=3,
            trust_env=False,
        )
        response.raise_for_status()
        if response.json().get("lease") or response.json().get("human_pending"):
            raise ValueError("Hand back the computer and finish the active browser task before switching.")
    except httpx.ConnectError:
        pass


@app.command()
def desktop_enable(
    runtime: Path = typer.Option(..., help="Path to Apple's container CLI."),
    image: str = typer.Option("", help="Desktop image tag or digest."),
):
    """Switch an idle native installation to its persistent Linux desktop."""
    from . import __version__

    root, config = configuration()
    if config.get("desktop", {}).get("enabled"):
        typer.echo("The desktop backend is already enabled.")
        return
    runtime = runtime.expanduser().resolve()
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        raise typer.BadParameter("Choose the installed Apple container executable.")
    image = image or f"adarshidigievo/shinbot:{__version__}-desktop"
    subprocess.run([str(runtime), "system", "status"], check=True, capture_output=True)
    subprocess.run([str(runtime), "image", "inspect", image], check=True, capture_output=True)
    idle_for_backend_change(root, config)
    stop()
    backup = root / "backups" / ("before-desktop-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    previous = dict(config)
    try:
        idle_for_backend_change(root, config)
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(root / "config.json", backup / "config.json")
        for name in ("control-data", "runtime-data", "computer-data", "browser-data"):
            if (root / name).exists():
                shutil.copytree(root / name, backup / name, symlinks=True)
        config["desktop"] = {"enabled": True, "runtime": str(runtime), "image": image}
        from .desktop_host import container_name, offline_copy

        volume = container_name(root) + "-data"
        if subprocess.run([str(runtime), "volume", "inspect", volume], capture_output=True).returncode:
            subprocess.run(
                [str(runtime), "volume", "create", "--opt", "size=20g", volume],
                check=True,
                capture_output=True,
            )
        offline_copy(root, config, root, restore_computer=True)
        temporary = root / "config.next.json"
        temporary.write_text(json.dumps(config))
        temporary.chmod(0o600)
        temporary.replace(root / "config.json")
        start()
        deadline = time.monotonic() + 90
        with httpx.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                try:
                    from .desktop_host import saved_endpoints

                    desktop_urls = saved_endpoints(root)
                    for name in ("computer", "browser"):
                        response = client.get(
                            desktop_urls.get(name, f"http://127.0.0.1:{config['ports'][name]}") + "/health",
                            headers={"Authorization": "Bearer " + config["tokens"][name]},
                        )
                        response.raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(1)
            else:
                raise RuntimeError(
                    "The desktop did not become healthy. Native execution is being restored; inspect native/logs/desktop.log."
                )
        typer.echo(f"Desktop enabled. Backup: {backup}. Browser sign-ins use a separate Linux profile.")
    except BaseException:
        stop()
        (root / "config.json").write_text(json.dumps(previous))
        start()
        raise


@app.command()
def desktop_backup():
    """Back up native settings, workspace and the stopped Linux desktop data."""
    root, config = configuration()
    if not config.get("desktop", {}).get("enabled"):
        raise typer.BadParameter("The desktop backend is not enabled.")
    idle_for_backend_change(root, config)
    stop()
    from .desktop_host import offline_copy

    destination = root / "backups" / ("desktop-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    try:
        idle_for_backend_change(root, config)
        destination.mkdir(parents=True, mode=0o700)
        shutil.copy2(root / "config.json", destination / "config.json")
        for name in ("control-data", "runtime-data", "computer-data", "browser-data", "workspace"):
            if (root / name).exists():
                shutil.copytree(root / name, destination / name, symlinks=True)
        offline_copy(root, config, destination)
        (destination / "complete").touch(mode=0o600)
    finally:
        start()
    typer.echo(f"Desktop backup saved: {destination}")


@app.command()
def desktop_disable():
    """Return to native execution; preserve desktop files and browser profiles."""
    root, config = configuration()
    if not config.get("desktop", {}).get("enabled"):
        typer.echo("Native execution is already selected.")
        return
    if config.get("computer_consent") != "macos-account":
        raise typer.BadParameter("Native execution needs explicit consent to use your macOS account.")
    idle_for_backend_change(root, config)
    stop()
    from .desktop_host import offline_copy

    backup = root / "backups" / ("before-native-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    try:
        idle_for_backend_change(root, config)
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(root / "config.json", backup / "config.json")
        for name in ("control-data", "runtime-data", "computer-data"):
            if (root / name).exists():
                shutil.copytree(root / name, backup / name, symlinks=True)
        offline_copy(root, config, backup)
        incoming = root / "computer-data.next"
        shutil.copytree(backup / "desktop-data/computer", incoming, symlinks=True)
        previous = root / "computer-data.previous"
        (root / "computer-data").rename(previous)
        try:
            incoming.rename(root / "computer-data")
        except BaseException:
            previous.rename(root / "computer-data")
            raise
        config["desktop"]["enabled"] = False
        temporary = root / "config.next.json"
        temporary.write_text(json.dumps(config))
        temporary.chmod(0o600)
        temporary.replace(root / "config.json")
        shutil.rmtree(previous)
    finally:
        start()
    typer.echo(f"Native execution restored. Desktop backup: {backup}.")


@app.command()
def telegram_setup():
    """Connect a private Telegram chat to the native installation."""
    from . import cli

    root, config = configuration()
    token, owner, username = cli.telegram_credentials()
    # Scan with this installation's data and interpreter before saving any credentials.
    try:
        subprocess.run(
            [config["python"], "-c", "import telegram\n" + cli.TELEGRAM_SCAN],
            env=service_environment(root, config),
            cwd=root,
            check=True,
            capture_output=True,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        raise typer.BadParameter(
            "Telegram dependency or security checks failed. Check the native Python environment "
            "has ShinBot's telegram and security extras. Settings were not changed."
        ) from None
    config["telegram"] = {"enabled": True, "bot_token": token, "owner_id": owner}
    cli.write_private(root / "config.json", json.dumps(config, indent=2) + "\n")
    stop()
    start()
    typer.echo(f"Telegram enabled. Open https://t.me/{username} and send /start, then /bots.")


@app.command()
def telegram_stop():
    """Stop native Telegram while preserving its settings and conversations."""
    from . import cli

    root, config = configuration()
    telegram = config.get("telegram", {})
    if telegram.get("enabled"):
        telegram["enabled"] = False
        cli.write_private(root / "config.json", json.dumps(config, indent=2) + "\n")
        stop()
        start()
    typer.echo("Telegram stopped. Run shinbot native telegram-setup to reconnect.")


if __name__ == "__main__":
    supervise()
