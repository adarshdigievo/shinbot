"""Manage one optional desktop container through Apple's public CLI."""

import hashlib
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time


def container_name(root):
    return "shinbot-desktop-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def endpoints(info):
    networks = info.get("status", {}).get("networks", [])
    for network in networks:
        if address := network.get("ipv4Address"):
            host = str(ipaddress.ip_interface(address).ip)
            return {"computer": f"http://{host}:8082", "browser": f"http://{host}:8083"}
    raise RuntimeError("The desktop has no network address yet.")


def saved_endpoints(root):
    path = root / "desktop-endpoints.json"
    return json.loads(path.read_text()) if path.exists() else {}


def offline_copy(root, config, destination, *, restore_computer=False):
    """Copy service data with the desktop stopped, using the guest filesystem."""
    runtime, image = config["desktop"]["runtime"], config["desktop"]["image"]
    name = container_name(root)
    found = subprocess.run([runtime, "inspect", name], capture_output=True, text=True)
    if found.returncode == 0 and json.loads(found.stdout)[0]["status"]["state"] == "running":
        raise RuntimeError("Stop the desktop before copying its service data.")
    if restore_computer:
        script = (
            "import os,shutil,tempfile; "
            "incoming=tempfile.mkdtemp(prefix='.computer-',dir='/data'); "
            "shutil.copytree('/transfer',incoming,dirs_exist_ok=True,symlinks=True); "
            "previous=incoming+'-previous'; "
            "os.rename('/data/computer',previous) if os.path.exists('/data/computer') else None; "
            "os.rename(incoming,'/data/computer'); "
            "shutil.rmtree(previous) if os.path.exists(previous) else None"
        )
    else:
        script = "import shutil; shutil.copytree('/data','/transfer/desktop-data',symlinks=True)"
    subprocess.run(
        [
            runtime,
            "run",
            "--rm",
            "--name",
            name + "-copy",
            "--memory",
            "512M",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--volume",
            f"{name}-data:/data" + ("" if restore_computer else ":ro"),
            "--volume",
            f"{destination / 'computer-data' if restore_computer else destination}:/transfer"
            + (":ro" if restore_computer else ""),
            image,
            "python",
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )


def create_command(root, config, env_file):
    desktop = config["desktop"]
    return [
        desktop["runtime"],
        "create",
        "--name",
        container_name(root),
        "--cpus",
        str(desktop.get("cpus", 4)),
        "--memory",
        desktop.get("memory", "4G"),
        "--env-file",
        str(env_file),
        "--cap-drop",
        "ALL",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "KILL",
        "--publish",
        f"127.0.0.1:{config['ports']['computer']}:8082",
        "--publish",
        f"127.0.0.1:{config['ports']['browser']}:8083",
        "--volume",
        f"{root / 'workspace'}:/workspace",
        "--volume",
        f"{container_name(root)}-data:/data",
        "--volume",
        f"{root / 'desktop-data/computer'}:/seed/computer:ro",
        desktop["image"],
    ]


def supervise(root, config):
    runtime = config["desktop"]["runtime"]
    name = container_name(root)
    env_file = root / "desktop.env"
    os.umask(0o077)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "desktop-data/computer").mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "\n".join(
            [
                "SHINBOT_COMPUTER_TOKEN=" + config["tokens"]["computer"],
                "SHINBOT_BROWSER_TOKEN=" + config["tokens"]["browser"],
                f"SHINBOT_WORKSPACE_UID={os.getuid()}",
                f"SHINBOT_WORKSPACE_GID={os.getgid()}",
                "SHINBOT_SHARED_WORKSPACE=1",
                "",
            ]
        )
    )
    env_file.chmod(0o600)
    volume = name + "-data"
    if subprocess.run([runtime, "volume", "inspect", volume], capture_output=True).returncode:
        subprocess.run([runtime, "volume", "create", "--opt", "size=20g", volume], check=True)
    found = subprocess.run([runtime, "inspect", name], capture_output=True)
    if not found.returncode and json.loads(found.stdout)[0].get("status", {}).get("state") != "running":
        subprocess.run([runtime, "delete", name], check=True, capture_output=True)
        found.returncode = 1
    if found.returncode:
        subprocess.run(create_command(root, config, env_file), check=True)
    # Only this installation's named container is managed here.
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state = subprocess.run([runtime, "inspect", name], capture_output=True, text=True, check=True)
    if json.loads(state.stdout)[0].get("status", {}).get("state") != "running":
        subprocess.run([runtime, "start", name], check=True)
    state = subprocess.run([runtime, "inspect", name], capture_output=True, text=True, check=True)
    temporary = root / "desktop-endpoints.next.json"
    temporary.write_text(json.dumps(endpoints(json.loads(state.stdout)[0])))
    temporary.replace(root / "desktop-endpoints.json")
    try:
        while not stopping:
            time.sleep(2)
            result = subprocess.run([runtime, "inspect", name], capture_output=True, text=True, timeout=10)
            if result.returncode or json.loads(result.stdout)[0].get("status", {}).get("state") != "running":
                return 1
    finally:
        (root / "desktop-endpoints.json").unlink(missing_ok=True)
        subprocess.run([runtime, "stop", "--time", "20", name], timeout=30, check=False)
    return 0


if __name__ == "__main__":
    from .native import configuration

    sys.exit(supervise(*configuration()))
