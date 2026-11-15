import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import typer

from . import __version__, maintenance
from .native import app as native_app

app = typer.Typer(
    help="ShinBot: your self-hosted personal assistant.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
app.add_typer(native_app, name="native")
plugins_app = typer.Typer(help="Install, inspect and manage plugin versions.")
marketplace_app = typer.Typer(help="Add repositories as plugin marketplaces.")
jobs_app = typer.Typer(help="Manage scheduled responsibilities.")
bots_app = typer.Typer(help="Create and export bot profiles and groups.")
app.add_typer(bots_app, name="bots")
notes_app = typer.Typer(help="Manage the shared notebook.")
app.add_typer(plugins_app, name="plugins")
app.add_typer(marketplace_app, name="marketplace")
app.add_typer(jobs_app, name="jobs")
app.add_typer(notes_app, name="notes")


@app.command()
def github_connect(url: str = typer.Option(...), code: str = typer.Option(...)):
    """Connect the GitHub CLI account to the pairing request shown in ShinBot."""
    from .connect_cli import connect_github

    connect_github(url, code)


@app.command()
def chatgpt_connect(url: str = typer.Option(...), code: str = typer.Option(...)):
    """Finish a remote ShinBot ChatGPT sign-in using a local browser callback."""
    from .connect_cli import connect_chatgpt

    connect_chatgpt(url, code)


def home():
    directory = Path(os.getenv("SHINBOT_HOME", "~/.shinbot")).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def client():
    file = home() / "client.json"
    return (
        json.loads(file.read_text())
        if file.exists()
        else {"url": deployment().get("SHINBOT_ORIGIN", "http://localhost:8765")}
    )


def deployment():
    file = home() / "deploy.env"
    if not file.exists():
        return {}
    return dict(
        line.split("=", 1)
        for line in file.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


def write_private(file, value):
    file = Path(file)
    descriptor, temporary = tempfile.mkstemp(prefix="." + file.name + "-", dir=file.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        directory = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def response_error(response):
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail[:1000]
    return f"Server returned HTTP {response.status_code}. Check your server address and try again."


def request(path, method="GET", body=None):
    saved = client()
    try:
        response = httpx.request(
            method,
            saved["url"] + path,
            json=body,
            timeout=120,
            headers={"Authorization": f"Bearer {saved.get('token', '')}"},
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        typer.echo(f"ShinBot: {response_error(exc.response)}", err=True)
        raise typer.Exit(1)
    except httpx.TransportError:
        typer.echo("Cannot reach ShinBot. Run shinbot status or check your server address.", err=True)
        raise typer.Exit(1)
    except ValueError:
        typer.echo("ShinBot returned an invalid response. Check your server address and try again.", err=True)
        raise typer.Exit(1)


def docker_engine(command):
    samples = []
    for _ in range(2):
        result = subprocess.run([*command, "info", "--format", "{{json .}}"], capture_output=True, text=True)
        try:
            info = json.loads(result.stdout)
        except (ValueError, TypeError):
            info = None
        if result.returncode or not isinstance(info, dict) or not info.get("ID"):
            raise typer.BadParameter("Start the Docker engine for this installation, then try again.")
        samples.append(info)
    if samples[0]["ID"] == samples[1]["ID"]:
        return samples[0]["ID"]
    # Podman's compatibility API generates a new ID for every info request.
    # Its host and storage directory are stable; the saved volume is also checked at start.
    identities = [(sample.get("Name"), sample.get("DockerRootDir")) for sample in samples]
    if identities[0] != identities[1] or not all(identities[0]):
        raise typer.BadParameter("The container engine identity changed while checking it. Try again.")
    return "storage-" + hashlib.sha256(json.dumps(identities[0]).encode()).hexdigest()


def docker():
    if not shutil.which("docker"):
        raise typer.BadParameter("Install a Docker-compatible runtime first.")
    saved = deployment()
    context = os.getenv("SHINBOT_DOCKER_CONTEXT") or saved.get("SHINBOT_DOCKER_CONTEXT")
    if not context:
        selected = subprocess.run(["docker", "context", "show"], capture_output=True, text=True)
        if selected.returncode or not selected.stdout.strip():
            raise typer.BadParameter("Choose a Docker context before starting ShinBot.")
        context = selected.stdout.strip()
    command = ["docker", "--context", context]
    engine = docker_engine(command)
    if saved.get("SHINBOT_DOCKER_ENGINE") and engine != saved["SHINBOT_DOCKER_ENGINE"]:
        raise typer.BadParameter(
            "This Docker engine differs from the saved installation. Start its original Docker engine "
            "and use the saved context. ShinBot has not created a new installation."
        )
    return command


def compose_file():
    explicit = os.getenv("SHINBOT_COMPOSE_FILE")
    if explicit:
        return Path(explicit).resolve()
    checkout = Path(__file__).resolve().parents[2] / "deploy/compose.yaml"
    if checkout.exists():
        return checkout
    return Path(__file__).parent / "deployment/compose.yaml"


def compose(*args, check=True, capture=False, configuration=None, timeout=None, input=None):
    if not (home() / "deploy.env").exists():
        raise typer.BadParameter("This installation has not been started. Run shinbot start first.")
    result = subprocess.run(
        [
            *docker(),
            "compose",
            "--project-name",
            deployment().get("SHINBOT_PROJECT", "shinbot"),
            "--env-file",
            str(home() / "deploy.env"),
            "-f",
            str(
                configuration
                or (
                    (home() / "rollback-compose.json")
                    if (home() / "rollback-compose.json").exists()
                    else compose_file()
                )
            ),
            *args,
        ],
        capture_output=capture,
        text=True,
        timeout=timeout,
        input=input,
        **maintenance.child_lock(),
    )
    if check and result.returncode:
        # Captured output can include service credentials; never echo it here.
        detail = "Run shinbot status to check the services." if capture else "Check the Docker output above."
        raise typer.BadParameter("Docker could not complete this command. " + detail)
    return result


@app.command()
@maintenance.guarded
def start(
    build: bool = False,
    telegram: bool = typer.Option(False, hidden=True),
    port: int | None = None,
    origin: str | None = None,
    open_browser: bool = True,
):
    """Start the persistent services. Use --build from a source checkout."""
    if build and not (compose_file().parent / "Dockerfile").exists():
        raise typer.BadParameter(
            "Building requires a ShinBot source checkout. Run uv run shinbot start --build there."
        )
    env = home() / "deploy.env"
    command = docker()
    values = deployment()
    if values:
        if (port is not None and str(port) != values.get("SHINBOT_PORT")) or (
            origin and origin != values.get("SHINBOT_ORIGIN")
        ):
            raise typer.BadParameter(
                "This installation already has a saved port and address. Edit SHINBOT_PORT and "
                "SHINBOT_ORIGIN in your SHINBOT_HOME/deploy.env, then run shinbot start."
            )
        if values.get("SHINBOT_INITIALIZED") != "0":
            configuration = json.loads(compose("config", "--format", "json", capture=True).stdout)
            volume = configuration["volumes"]["control-data"]["name"]
            exists = (
                subprocess.run([*command, "volume", "inspect", volume], capture_output=True).returncode == 0
            )
            if not exists:
                raise typer.BadParameter(
                    "The saved installation's data volume is missing from this Docker engine. "
                    "Use its original Docker context or restore a backup. ShinBot has not created empty data."
                )
    else:
        values = {
            name: secrets.token_urlsafe(40)
            for name in [
                "SHINBOT_SERVICE_TOKEN",
                "SHINBOT_COMPUTER_TOKEN",
                "SHINBOT_BROWSER_TOKEN",
            ]
        }
        values.update(
            {
                "SHINBOT_PROJECT": os.getenv("SHINBOT_PROJECT", "shinbot"),
                "SHINBOT_PORT": str(port or 8765),
                "SHINBOT_ORIGIN": origin or f"http://localhost:{port or 8765}",
                "SHINBOT_INITIALIZED": "0",
            }
        )
    values.update(SHINBOT_DOCKER_CONTEXT=command[2], SHINBOT_DOCKER_ENGINE=docker_engine(command))
    write_private(env, "\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    try:
        compose("up", "-d", *(["--build"] if build else []))
    finally:
        # A partial first start can create usable data even when another service fails.
        configuration = compose("config", "--format", "json", capture=True, check=False)
        if configuration.returncode == 0:
            volume = json.loads(configuration.stdout)["volumes"]["control-data"]["name"]
            if subprocess.run([*command, "volume", "inspect", volume], capture_output=True).returncode == 0:
                values["SHINBOT_INITIALIZED"] = "1"
                write_private(env, "\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    maintenance.remember_release()
    retire_telegram_service(command)
    if telegram:
        telegram_moved()
    address = values.get("SHINBOT_ORIGIN", f"http://localhost:{port or 8765}")
    typer.echo(f"ShinBot is starting at {address}.")
    if open_browser:
        import webbrowser

        for _ in range(30):
            try:
                result = compose(
                    "exec",
                    "-T",
                    "control",
                    "python",
                    "-c",
                    "import urllib.request,json; from pathlib import Path; r=urllib.request.urlopen('http://127.0.0.1:8765/auth/status',timeout=2); print('' if json.load(r)['claimed'] else Path('/data/bootstrap-token').read_text().strip())",
                    capture=True,
                    check=False,
                )
                if result.returncode:
                    time.sleep(1)
                    continue
                setup = result.stdout.strip()
                if webbrowser.open(address + ("/#setup=" + setup if setup else "")):
                    typer.echo("Opened ShinBot in your browser. Finish setup on Home.")
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            typer.echo(
                "Open the address above. Run shinbot setup-token if Home asks for an installation token."
            )
    else:
        typer.echo("Open the address above. Run shinbot setup-token if this installation is new.")


@app.command()
def setup_token():
    """Show the one-time installation token on this host."""
    result = compose("exec", "-T", "control", "cat", "/data/bootstrap-token", capture=True)
    typer.echo(result.stdout)


# Runs beside the gateway's data. Exit 3 carries a message for the owner; anything else is the host's.
RESET_PASSWORD = """
import sys
from shinbot.auth import hash_password
from shinbot.config import Config
from shinbot.db import Database, packed
path = Config().data / 'shinbot.sqlite'
db = Database(path) if path.is_file() else None
if db is None or db.setting('password') is None:
    print('This installation has no owner yet. Run shinbot setup-token and finish setup in the browser.')
    sys.exit(3)
encoded = hash_password(sys.stdin.readline().rstrip('\\n'))
with db.transaction() as sql:
    sql.execute("UPDATE settings SET value=? WHERE key='password'", (packed(encoded),))
    sql.execute('DELETE FROM sessions')
"""


@app.command()
def reset_password(native: bool = False):
    """Set a new owner password from this host and sign out every device."""
    from . import native as host

    native = native or (not (home() / "deploy.env").exists() and (host.directory() / "config.json").is_file())
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    if not 12 <= len(password) <= 256 or "\n" in password:
        raise typer.BadParameter("Use a password between 12 and 256 characters.")
    # The password travels on standard input, never in a command line or the environment.
    if native:
        try:
            root, config = host.configuration()
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        result = subprocess.run(
            [config["python"], "-c", RESET_PASSWORD],
            env=host.service_environment(root, config),
            cwd=root,
            input=password + "\n",
            capture_output=True,
            text=True,
        )
    else:
        result = compose(
            "exec", "-T", "control", "python", "-c", RESET_PASSWORD, capture=True, check=False, input=password + "\n"
        )
    if result.returncode == 3:
        raise typer.BadParameter(result.stdout.strip())
    if result.returncode:
        raise typer.BadParameter(
            "The password was not changed. Check that ShinBot is running on this host, then try again."
        )
    typer.echo("Password reset. Every device was signed out. Sign in with the new password.")


@app.command()
@maintenance.guarded
def stop():
    """Stop services and preserve their data."""
    compose("stop")


@app.command()
def status():
    """Show the local services."""
    compose("ps")


@app.command()
def doctor():
    """Check the container runtime, services and web endpoint."""
    typer.echo(f"ShinBot {__version__}")
    compose("config", "--quiet")
    compose("ps")
    try:
        response = httpx.get(client()["url"] + "/health", timeout=5)
        response.raise_for_status()
        typer.echo("Gateway responds. Sign in and check Settings for provider status.")
    except httpx.HTTPError:
        typer.echo("Gateway is not reachable yet.", err=True)
        raise typer.Exit(1)


@app.command()
def login(url: str | None = None):
    """Connect this CLI to a local or remote ShinBot gateway."""
    url = url or client()["url"]
    parsed = urlparse(url)
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise typer.BadParameter("Use HTTPS for remote servers, or an SSH tunnel to localhost.")
    password = typer.prompt("Password", hide_input=True)
    try:
        response = httpx.post(
            url.rstrip("/") + "/auth/login", json={"password": password, "cli": True}, timeout=30
        )
    except httpx.TransportError:
        typer.echo("Cannot reach ShinBot. Run shinbot status or check your server address.", err=True)
        raise typer.Exit(1)
    if response.status_code != 200:
        typer.echo(f"Sign-in failed. {response_error(response)}", err=True)
        raise typer.Exit(1)
    try:
        body = response.json()
    except ValueError:
        body = None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        typer.echo("Sign-in returned an invalid response. Check your server address and try again.", err=True)
        raise typer.Exit(1)
    write_private(home() / "client.json", json.dumps({"url": url.rstrip("/"), "token": token}))
    typer.echo("Connected.")


@app.command()
def chat(prompt: str, task: str | None = None, wait: bool = True, bot: str = "default"):
    """Ask ShinBot a question, optionally continuing a task."""
    message_id = None
    if task:
        message_id = request(
            f"/api/tasks/{task}/messages", "POST", {"content": prompt, "request_id": secrets.token_hex(16)}
        )["id"]
    else:
        task = request("/api/tasks", "POST", {"content": prompt, "bot_id": bot})["id"]
    typer.echo(f"Task: {task}")
    if not wait:
        return
    while True:
        detail = request(f"/api/tasks/{task}")
        message = (
            next((m for m in detail["messages"] if m["id"] == message_id), None)
            if message_id
            else detail["messages"][0]
        )
        if (message and message["status"] in {"completed", "failed", "cancelled"}) or detail["status"] in {
            "cancelled",
            "uncertain",
        }:
            typer.echo((message or {}).get("answer") or detail.get("error") or detail["status"])
            return
        if detail["status"] == "waiting":
            typer.echo(f"Waiting for your approval in {client()['url']}.")
            return
        time.sleep(1)


@plugins_app.command("access")
def plugin_access(plugin: str, bot: list[str] = typer.Option(None), shared: bool = False):
    """Choose which bots can use a plugin, MCP connection or skill."""
    if shared == bool(bot):
        raise typer.BadParameter("Choose --shared or one or more --bot IDs.")
    result = request(
        f"/api/plugins/{plugin}/access", "PUT", {"mode": "shared" if shared else "bots", "bots": bot or []}
    )
    typer.echo(json.dumps(result))


@plugins_app.command("connect-mcp")
def connect_mcp(source: Path, bot: str = ""):
    """Connect from a JSON file containing id, name, version and server configuration."""
    payload = json.loads(source.read_text())
    if bot:
        payload["access"] = {"mode": "bots", "bots": [bot]}
    result = request("/api/plugins/mcp/connect", "POST", payload)
    typer.echo(f"Connected {result['id']} {result['version']}. Review and activate it in Plugins.")


@plugins_app.command("install-skills")
def install_skills(command: str, license: str, bot: str = ""):
    """Run an npx skills add command in the Computer service and stage the result."""
    payload = {
        "command": command,
        "license": license,
        "access": {"mode": "bots", "bots": [bot]} if bot else {"mode": "shared", "bots": []},
    }
    typer.echo(json.dumps(request("/api/plugins/skills/install", "POST", payload)))


@plugins_app.command("import-skill")
def import_skill(source: Path, license: str, bot: str = ""):
    """Import a local skill, including one installed with npx skills add."""
    files, size = {}, 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise typer.BadParameter("Skill files must not be symbolic links. Use npx skills add --copy.")
        if path.is_file():
            size += path.stat().st_size
            if size > 350000 or len(files) >= 90:
                raise typer.BadParameter("Skill exceeds the 350 KB or 90-file import limit.")
            try:
                files[path.relative_to(source).as_posix()] = path.read_text()
            except UnicodeDecodeError:
                raise typer.BadParameter(
                    "This skill contains binary files. Import a text-only skill package; binary assets are not supported yet."
                ) from None
    result = request(
        "/api/plugins/skills/import",
        "POST",
        {
            "files": files,
            "license": license,
            "access": {"mode": "bots", "bots": [bot]} if bot else {"mode": "shared", "bots": []},
        },
    )
    typer.echo(json.dumps(result))


@plugins_app.command("list")
def plugin_list():
    typer.echo(json.dumps(request("/api/plugins")["installed"], indent=2))


@plugins_app.command("install")
def plugin_install(source: Path):
    """Validate and stage a local plugin directory."""
    manifest = json.loads((source / "shinbot-plugin.json").read_text())
    files = {name: (source / name).read_text() for name in ["shinbot-plugin.json", *manifest["files"]]}
    result = request("/api/plugins/stage", "POST", {"files": files})
    typer.echo(f"Staged {result['id']} {result['version']}. Activate it after reviewing its permissions.")


@plugins_app.command("activate")
def plugin_activate(plugin: str, version: str):
    """Activate an installed version; choose an older version to roll back."""
    request(f"/api/plugins/{plugin}/{version}/activate", "POST", {})
    typer.echo("Activated for new tasks.")


@plugins_app.command("configure")
def plugin_configure(plugin: str, version: str):
    """Set plugin credentials and options without putting secrets in shell history."""
    path = f"/api/plugins/{plugin}/{version}/configuration"
    fields = request(path)["fields"]
    if not fields:
        typer.echo("This plugin has no settings.")
        return
    values = {}
    for field in fields:
        typer.echo(field["description"])
        label = field["label"] + (" (leave blank to keep saved value)" if field["configured"] else "")
        value = typer.prompt(label, hide_input=field["secret"], default="", show_default=False)
        if value:
            values[field["key"]] = value
    request(path, "PUT", values)
    typer.echo("Plugin settings saved.")


@plugins_app.command("disable")
def plugin_disable(plugin: str):
    request(f"/api/plugins/{plugin}/disable", "POST", {})
    typer.echo("Disabled.")


@marketplace_app.command("add")
def market_add(repository: str, ref: str = "HEAD"):
    result = request("/api/marketplaces", "POST", {"source": repository, "ref": ref})
    typer.echo(f"Added {repository} at {result['revision']}.")


@marketplace_app.command("list")
def market_list():
    typer.echo(json.dumps(request("/api/plugins")["marketplaces"], indent=2))


@notes_app.command("list")
def note_list(query: str = ""):
    from urllib.parse import quote

    typer.echo(json.dumps(request("/api/notes?q=" + quote(query)), indent=2))


@notes_app.command("add")
def note_add(title: str, body: str):
    typer.echo(request("/api/notes", "POST", {"title": title, "body": body})["id"])


@jobs_app.command("list")
def job_list():
    typer.echo(json.dumps(request("/api/jobs"), indent=2))


@jobs_app.command("add")
def job_add(name: str, prompt: str, cron: str, timezone: str = "UTC"):
    typer.echo(
        request(
            "/api/jobs",
            "POST",
            {"name": name, "prompt": prompt, "kind": "cron", "spec": cron, "timezone": timezone},
        )["id"]
    )


@jobs_app.command("pause")
def job_pause(job: str):
    request(f"/api/jobs/{job}/toggle", "POST", {"enabled": False})


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Run the Python gateway for development; private services must already be running."""
    import uvicorn

    uvicorn.run("shinbot.app:create_app", factory=True, host=host, port=port, access_log=False)


@app.command()
def version():
    typer.echo(__version__)


def volume_operation(operation, file, configuration=None, trusted=False):
    configuration = configuration or json.loads(compose("config", "--format", "json", capture=True).stdout)
    command = [
        *docker(),
        "run",
        "--rm",
        "--user",
        "0",
        "--network",
        "none",
        "-e",
        f"SHINBOT_BACKUP_UID={os.getuid()}",
        "-e",
        f"SHINBOT_BACKUP_GID={os.getgid()}",
        # This installation's own archives may hold more files than one from elsewhere.
        *(["-e", "SHINBOT_BACKUP_TRUSTED=1"] if trusted else []),
    ]
    for key, value in configuration["volumes"].items():
        command += ["--mount", f"type=volume,source={value['name']},target=/volumes/{key}"]
    command += [
        "--mount",
        f"type=bind,source={file.parent.resolve()},target=/backup",
        "--entrypoint",
        "python",
        configuration["services"]["computer"]["image"],
        "-m",
        "shinbot.volume_admin",
        operation,
        "/backup/" + file.name,
    ]
    subprocess.run(command, check=True, **maintenance.child_lock())


@app.command()
def backup(destination: Path):
    """Back up settings, plugins and all persistent data. Keep the archive private."""
    try:
        maintenance.backup(destination.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise typer.BadParameter(
            "Backup could not finish. Any reason is printed above; otherwise check disk space and Docker."
        ) from exc


@app.command()
def backup_info(source: Path):
    """Verify a backup and show its format and creation time without exposing secrets."""
    try:
        with maintenance.unpack(source) as (_, root, manifest):
            typer.echo(
                json.dumps(
                    {k: manifest[k] for k in ("format", "version", "created") if k in manifest}, indent=2
                )
            )
            typer.echo(
                "Verified: all five data volumes" + (", host settings and release metadata." if root else ".")
            )
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise typer.BadParameter(
            "Backup is unreadable, incomplete or damaged. No installation data changed."
        ) from exc


@app.command()
def restore(source: Path, replace: bool = False, trusted: bool = False):
    """Restore a full backup; preserve this host's address and hold automated work for review.

    Use --trusted for a very large backup that you made yourself."""
    if not replace:
        raise typer.BadParameter(
            "Use --replace to replace this installation's data. A pre-restore backup is saved first."
        )
    try:
        maintenance.restore(source, trusted)
    except ValueError as exc:
        # Validation messages name what is wrong with the archive and hold no secrets.
        raise typer.BadParameter(f"Restore could not finish. {exc}") from exc
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise typer.BadParameter(
            "Restore could not finish. Check the backup and Docker. See any recovery path printed above."
        ) from exc


@app.command()
def update(
    build: bool = False,
    timeout: int = typer.Option(120, min=5, max=900),
    check: bool = False,
    latest: bool = False,
):
    """Check releases or apply an update with a full restore point and automatic fallback."""
    if sum((build, check, latest)) > 1:
        raise typer.BadParameter("Choose only one of --build, --check or --latest.")
    if check:
        from .updates import discover

        result = asyncio.run(discover())
        typer.echo(f"Installed launcher: {__version__}\n{result['message']}\n{result['release_url']}")
        if result["status"] == "available":
            typer.echo("Run shinbot update --latest on the installation host to update.")
        return
    if latest:
        try:
            maintenance.update_latest(timeout)
        except typer.BadParameter:
            raise
        except (
            OSError,
            ValueError,
            LookupError,
            ImportError,
            subprocess.SubprocessError,
            RuntimeError,
        ) as exc:
            raise typer.BadParameter(
                "Update could not finish. Check the saved release, Python installer and Docker. If recovery is pending, run shinbot rollback; otherwise retry shinbot update --latest."
            ) from exc
        return
    try:
        maintenance.update(build, timeout)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        raise typer.BadParameter(
            "Update could not finish. Check Docker and disk space. If recovery is pending, run shinbot rollback; otherwise retry shinbot update."
        ) from exc


@app.command()
def rollback(timeout: int = typer.Option(120, min=5, max=900)):
    """Return to the previous release and data, even when the web app cannot start."""
    try:
        maintenance.rollback(timeout)
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError, RuntimeError) as exc:
        raise typer.BadParameter(
            "Rollback could not finish. Check Docker, free disk space and the saved images and backups, then retry shinbot rollback. Automated work remains held after data is restored."
        ) from exc


def running_services():
    known = {"control", "runtime", "computer", "browser"}
    result = compose("ps", "--services", "--status", "running", capture=True)
    return [name for name in result.stdout.split() if name in known]


def telegram_moved():
    typer.echo(
        "Telegram now runs inside ShinBot and needs no host setup. "
        "Open Plugins → Telegram in the web app to connect, change or disable it."
    )


def retire_telegram_service(command):
    """Remove the separate Telegram container that releases before the in-app connector started."""
    project = deployment().get("SHINBOT_PROJECT", "shinbot")
    found = subprocess.run(
        [
            *command,
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=telegram",
        ],
        capture_output=True,
        text=True,
    )
    if found.returncode == 0 and found.stdout.split():
        subprocess.run([*command, "rm", "-f", *found.stdout.split()], capture_output=True)


@app.command(hidden=True)
def telegram_setup():
    """Telegram is now connected from the web app."""
    telegram_moved()


@app.command(hidden=True)
def telegram_stop():
    """Telegram is now disabled from the web app."""
    telegram_moved()


@bots_app.command("list")
def bots_list():
    typer.echo(json.dumps(request("/api/bots"), indent=2))


@bots_app.command("create")
def bot_create(name: str, instructions: str = "", browser: str = "private"):
    result = request(
        "/api/bots", "POST", {"profile": {"name": name, "instructions": instructions, "browser": browser}}
    )
    typer.echo(result["id"])


@bots_app.command("export")
def bot_export(bot: str, destination: Path):
    if destination.exists():
        raise typer.BadParameter("Choose a new template filename.")
    write_private(destination, json.dumps(request(f"/api/bots/{bot}/export"), indent=2) + "\n")
    typer.echo(
        "Exported profile settings only. Review the instructions for private information before sharing."
    )


@bots_app.command("import")
def bot_import(template: Path):
    result = request(
        "/api/templates/install",
        "POST",
        {"template": json.loads(template.read_text()), "request_id": secrets.token_hex(16)},
    )
    typer.echo(json.dumps(result, indent=2))


@bots_app.command("add-repository")
def template_repository(repository: str):
    result = request("/api/templates/repositories", "POST", {"source": repository})
    typer.echo(f"Added {repository} at {result['revision']}.")


if __name__ == "__main__":
    app()
