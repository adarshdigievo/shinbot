"""Optional browser engines behind ShinBot's leases, approvals and effect journal."""

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

BACKENDS = {"chromium": "Chromium", "camoufox": "Camoufox", "agent-browser": "agent-browser"}
AGENT_VERSION = "0.41.0"


def agent_executable():
    configured = os.getenv("SHINBOT_AGENT_BROWSER_EXECUTABLE")
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    # The published native CLI does not require a Node runtime.
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(platform.machine())
    roots = [Path(__file__).resolve().parents[3] / "browser", Path(sys.prefix) / "share/shinbot/browser"]
    for root in roots:
        path = root / "node_modules/agent-browser/bin" / f"agent-browser-{system}-{arch}"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("agent-browser")


def camoufox_executable():
    from camoufox.pkgman import camoufox_path, launch_path

    # Never download executables as a side effect of viewing settings or opening a page.
    return launch_path(camoufox_path(download_if_missing=False))


def catalog(playwright):
    chromium = Path(playwright.chromium.executable_path).is_file()
    cli_version = agent_version()
    rows = [
        {
            "id": "chromium",
            "name": "Chromium",
            "ready": chromium,
            "setup": "python -m playwright install chromium",
        },
        {
            "id": "camoufox",
            "name": "Camoufox",
            "ready": False,
            "setup": "uv pip install 'shinbot[camoufox]' && python -m camoufox fetch",
        },
        {
            "id": "agent-browser",
            "name": "agent-browser",
            "ready": cli_version == AGENT_VERSION and chromium,
            "version": cli_version,
            "setup": f"npm install -g agent-browser@{AGENT_VERSION} && python -m playwright install chromium",
        },
    ]
    try:
        rows[1]["version"] = version("camoufox")
        rows[1]["ready"] = bool(camoufox_executable())
    except Exception:
        pass
    return rows


def agent_version():
    executable = agent_executable()
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip().removeprefix("agent-browser ") if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def profile_path(data, backend, profile):
    # Keep the existing Chromium directories intact. Firefox and the CLI get separate stores.
    root = data if backend == "chromium" else data / "backends" / backend
    return root / "profile" if profile == "shared" else root / "profiles" / profile


@dataclass
class AgentBrowser:
    executable: str
    endpoint: str
    config: Path
    session: str

    async def call(self, tool, arguments):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_BROWSER_", "SHINBOT_"))}
        env.update(
            AGENT_BROWSER_CONFIG=str(self.config),
            AGENT_BROWSER_SESSION=self.session,
            AGENT_BROWSER_SOCKET_DIR=str(self.config.parent / "agent-sockets"),
        )
        (self.config.parent / "agent-sockets").mkdir(mode=0o700, exist_ok=True)
        arguments = {**arguments, "session": self.session, "timeoutMs": 35000}
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "--config",
            str(self.config),
            "--session",
            self.session,
            "--cdp",
            self.endpoint,
            "mcp",
            "call",
            "agent_browser_" + tool,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.config.parent,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(arguments).encode()), 45)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError):
            result = {"isError": True}
        if process.returncode or result.get("isError"):
            # Upstream errors can echo text entered into password fields. Do not log or forward them.
            raise RuntimeError(
                "agent-browser could not confirm this action. Inspect the page before retrying."
            )
        return result

    async def execute(self, page, operation, args):
        # Target IDs survive tab reorder and keep the CLI on the exact page assessed by ShinBot.
        cdp = await page.context.new_cdp_session(page)
        try:
            target = (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await cdp.detach()
        await self.call("tab_switch", {"tab": target})
        if operation == "goto":
            await self.call("open", {"url": args["url"]})
        elif operation in {"click", "fill"}:
            arguments = {"selector": args["selector"]}
            if operation == "fill":
                arguments["text"] = args["text"]
            await self.call(operation, arguments)
        elif operation == "press":
            await page.locator(args.get("selector", "body")).focus()
            await self.call("press", {"key": args["key"]})
        elif operation == "back":
            await self.call("back", {})

    async def close(self):
        await self.call("close", {})


async def launch(playwright, backend, data, profile, headless):
    if backend not in BACKENDS:
        raise ValueError("Choose Chromium, Camoufox or agent-browser in Settings → Browser.")
    directory = profile_path(data, backend, profile)
    directory.mkdir(parents=True, exist_ok=True)
    desktop = os.getenv("SHINBOT_DESKTOP") == "1"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("SHINBOT_", "TELEGRAM_"))
    }
    if desktop:
        uid, gid = int(os.environ["SHINBOT_WORKSPACE_UID"]), int(os.environ["SHINBOT_WORKSPACE_GID"])
        for parent in (directory, *directory.parents):
            if parent == data:
                break
            os.chown(parent, uid, gid)
        environment.update(SHINBOT_WORKSPACE_UID=str(uid), SHINBOT_WORKSPACE_GID=str(gid))
    options = dict(
        user_data_dir=str(directory),
        headless=headless,
        viewport={"width": 1280, "height": 800},
        accept_downloads=True,
        downloads_path="/workspace/Downloads" if desktop else str(data / "downloads"),
        env=environment,
    )
    if backend == "camoufox":
        from camoufox.addons import DefaultAddons
        from camoufox.async_api import AsyncNewBrowser
        from camoufox.utils import launch_options

        executable = camoufox_executable()
        if desktop:
            environment["SHINBOT_BROWSER_BINARY"] = str(executable)
        # Resolve Camoufox's adjacent configuration against the real installation.
        # The public from_options API lets Playwright launch it through our UID wrapper.
        resolved = await asyncio.to_thread(
            launch_options,
            executable_path=executable,
            exclude_addons=list(DefaultAddons),
            pin_cpu_cores=False,
            **options,
        )
        if desktop:
            resolved["executable_path"] = "/usr/local/bin/shinbot-desktop-browser"
        context = await AsyncNewBrowser(playwright, persistent_context=True, from_options=resolved)
        return context, None
    args = ["--disable-dev-shm-usage"]
    if desktop:
        environment["SHINBOT_BROWSER_BINARY"] = playwright.chromium.executable_path
        options["executable_path"] = "/usr/local/bin/shinbot-desktop-browser"
    if backend == "agent-browser":
        executable = agent_executable()
        if not executable or await asyncio.to_thread(agent_version) != AGENT_VERSION:
            raise ValueError(
                f"Install agent-browser {AGENT_VERSION} on the browser service host first. See Settings → Browser."
            )
        # Chromium picks a free loopback port and writes it inside its private profile directory.
        args += ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"]
    context = await playwright.chromium.launch_persistent_context(args=args, **options)
    if backend != "agent-browser":
        return context, None
    try:
        port = int((directory / "DevToolsActivePort").read_text().splitlines()[0])
        config = data / "agent-browser.json"
        config.write_text(json.dumps({"cdp": f"http://127.0.0.1:{port}", "idleTimeout": "5m"}) + "\n")
        driver = AgentBrowser(
            executable, f"http://127.0.0.1:{port}", config, "shinbot-" + uuid.uuid4().hex[:16]
        )
        return context, driver
    except BaseException:
        await context.close()
        raise
