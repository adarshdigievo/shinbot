"""Supervise the optional desktop container; never run on the host."""

import grp
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    if sys.platform != "linux" or os.getuid() != 0 or os.getenv("SHINBOT_DESKTOP") != "1":
        raise SystemExit("The desktop runner requires the ShinBot Linux desktop image.")
    uid = int(os.getenv("SHINBOT_WORKSPACE_UID", "1001"))
    gid = int(os.getenv("SHINBOT_WORKSPACE_GID", "1001"))
    if uid < 1 or gid < 1:
        raise SystemExit("The desktop user must not be root.")
    try:
        grp.getgrgid(gid)
    except KeyError:
        subprocess.run(["groupadd", "-g", str(gid), "desktop-user"], check=True)
    subprocess.run(["usermod", "-u", str(uid), "-g", str(gid), "-d", "/workspace", "workspace"], check=True)
    os.umask(0o077)
    workspace = Path(os.environ["SHINBOT_WORKSPACE"])
    browser = Path(os.environ["SHINBOT_BROWSER_DATA"])
    computer = Path(os.environ["SHINBOT_EXECUTOR_DATA"])
    state = Path(os.environ["SHINBOT_DESKTOP_STATE"])
    for path in (workspace, browser, computer, state):
        path.mkdir(parents=True, exist_ok=True)
        if path != workspace or os.getenv("SHINBOT_SHARED_WORKSPACE") != "1":
            os.chown(path, uid, gid)
    # The terminal user can use browser profiles but cannot replace executor receipts.
    for path in (browser, computer):
        os.chown(path, 0, 0)
        path.chmod(0o755)
    os.chown(state, 0, 0)
    state.chmod(0o755)
    session = state / "session"
    session.mkdir(mode=0o700, exist_ok=True)
    os.chown(session, uid, gid)
    for name in ("Desktop", "Downloads"):
        path = workspace / name
        path.mkdir(exist_ok=True)
        if os.getenv("SHINBOT_SHARED_WORKSPACE") != "1":
            os.chown(path, uid, gid)
    seed = Path("/seed/computer")
    if seed.is_dir() and not (computer / ".seeded").exists():
        shutil.copytree(seed, computer, dirs_exist_ok=True, symlinks=True)
        for path in computer.rglob("*"):
            if not path.is_symlink():
                os.chown(path, 0, 0)
        (computer / ".seeded").touch()
    (state / "owner.json").unlink(missing_ok=True)
    (state / "access.lock").touch()
    os.chown(state / "access.lock", 0, 0)
    password = browser / "view-password"
    if not password.exists():
        password.write_text(secrets.token_hex(4))
        os.chown(password, uid, gid)
    vnc = state / "vnc-password"
    vnc.write_bytes(subprocess.check_output(["vncpasswd", "-f"], input=password.read_bytes()))
    os.chown(vnc, uid, gid)
    # Desktop applications never inherit ShinBot's service credentials.
    user_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(workspace),
        "DISPLAY": ":99",
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": str(session),
    }
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    def start(args, env, user=False):
        process = subprocess.Popen(
            args,
            env=env,
            start_new_session=True,
            **({"user": uid, "group": gid, "extra_groups": []} if user else {}),
        )
        children.append(process)
        return process

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        start(["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"], user_env, True)
        for _ in range(100):
            if Path("/tmp/.X11-unix/X99").exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Desktop display did not start.")
        start(["dbus-run-session", "--", "startxfce4"], user_env, True)
        for port, writable in ((5900, "0"), (5901, "1")):
            start(
                [
                    "X0tigervnc",
                    "-display",
                    ":99",
                    "-rfbport",
                    str(port),
                    "-localhost",
                    "-SecurityTypes",
                    "VncAuth",
                    "-PasswordFile",
                    str(vnc),
                    "-AlwaysShared",
                    f"-AcceptKeyEvents={writable}",
                    f"-AcceptPointerEvents={writable}",
                    "-AcceptSetDesktopSize=0",
                ],
                user_env,
                True,
            )
            start(["websockify", f"127.0.0.1:{6080 + port - 5900}", f"127.0.0.1:{port}"], user_env, True)
        for service, port in (("browser", 8083), ("computer", 8082)):
            env = {
                **os.environ,
                **user_env,
                "SHINBOT_EXECUTOR_TOKEN": os.environ[f"SHINBOT_{service.upper()}_TOKEN"],
            }
            for key in ("SHINBOT_COMPUTER_TOKEN", "SHINBOT_BROWSER_TOKEN"):
                env.pop(key, None)
            start(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    f"shinbot.services.{service}:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(port),
                    "--no-access-log",
                ],
                env,
                False,
            )
        while not stopping and all(p.poll() is None for p in children):
            time.sleep(0.5)
    finally:
        for process in reversed(children):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in children:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return 0 if stopping else 1


if __name__ == "__main__":
    sys.exit(main())
