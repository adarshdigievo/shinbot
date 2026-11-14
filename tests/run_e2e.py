"""Provision an isolated Docker deployment and run the named integration journeys.

Use --prepare-only to leave a test deployment for manual review. The printed
context.env can be sourced to run a single journey. --cleanup removes only that
runner-owned deployment. No personal installation is ever selected by default.
"""

import argparse
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOURNEYS = (
    "projects",
    "updates",
    "integration",
    "extended",
    "recovery",
    "profiles",
    "restore",
    "maintenance",
    "learning",
    "reinstall",
    "ui",
    "browser_preview",
)


def environment(home):
    values = json.loads((home / "test-deployment.json").read_text())
    env = {key: value for key, value in os.environ.items() if not key.startswith("SHINBOT_")}
    if os.environ.get("SHINBOT_DOCKER_CONTEXT"):
        env["SHINBOT_DOCKER_CONTEXT"] = os.environ["SHINBOT_DOCKER_CONTEXT"]
    env.update(
        SHINBOT_HOME=str(home),
        SHINBOT_COMPOSE_FILE=str(home / "test-compose.json"),
        SHINBOT_TEST_HOME=str(home),
        SHINBOT_TEST_PROJECT=values["project"],
        SHINBOT_TEST_URL=values["url"],
        SHINBOT_TEST_STATE=str(home / "owner.json"),
    )
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", choices=JOURNEYS, default=list(JOURNEYS))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--image-repository",
        help="Local image repository for a new test deployment (default: shinbot-dev/shinbot)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="Keep this test deployment and private context after checks"
    )
    parser.add_argument("--context", type=Path, help="Reuse a runner-created private test home")
    parser.add_argument(
        "--cleanup", action="store_true", help="Remove the test project in --context, including its volumes"
    )
    args = parser.parse_args()
    if args.cleanup and not args.context:
        parser.error("--cleanup requires --context")
    if args.context and args.image_repository:
        parser.error("An existing test context keeps its saved image repository.")
    home = args.context.resolve() if args.context else Path(tempfile.mkdtemp(prefix="shinbot-test-"))
    if not args.context:
        project = "shinbot-test-" + secrets.token_hex(6)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        values = {"project": project, "url": f"http://localhost:{port}"}
        (home / "test-deployment.json").write_text(json.dumps(values))
        deploy = {
            "SHINBOT_PROJECT": project,
            "SHINBOT_PORT": str(port),
            "SHINBOT_BIND": "127.0.0.1",
            "SHINBOT_ORIGIN": values["url"],
            "SHINBOT_IMAGE": args.image_repository or "shinbot-dev/shinbot",
        }
        deploy.update(
            {
                "SHINBOT_" + name + "_TOKEN": secrets.token_urlsafe(48)
                for name in ("SERVICE", "COMPUTER", "BROWSER", "CHANNEL")
            }
        )
        (home / "deploy.env").write_text("\n".join(f"{key}={value}" for key, value in deploy.items()) + "\n")
        (home / "deploy.env").chmod(0o600)
    env = environment(home)
    context_file = home / "context.env"
    context_file.write_text(
        "\n".join(
            f"export {key}={shlex.quote(value)}" for key, value in env.items() if key.startswith("SHINBOT_")
        )
        + "\n"
    )
    context_file.chmod(0o600)
    for key in list(os.environ):
        if key.startswith("SHINBOT_"):
            os.environ.pop(key)
    os.environ.update({key: value for key, value in env.items() if key.startswith("SHINBOT_")})
    from integration_support import COMPOSE, URL, connect, fixture_settings, ready

    def cleanup():
        subprocess.run([*COMPOSE, "down", "--volumes", "--remove-orphans"], check=True, env=env)
        shutil.rmtree(home)

    if args.cleanup:
        cleanup()
        return
    # CLI recovery must use the same fixture overlay as the Docker test deployment.
    from shinbot.maintenance import compose_document

    merged = subprocess.run(
        [*COMPOSE, "config", "--format", "json"], check=True, capture_output=True, text=True, env=env
    )
    compose_document(home / "test-compose.json", json.loads(merged.stdout))
    keep = args.keep or args.prepare_only
    try:
        subprocess.run(
            [*COMPOSE, "up", "-d", "--no-build", "control", "runtime", "computer", "browser", "fixture"],
            check=True,
            env=env,
        )
        client = connect()
        try:
            ready(client)
            fixture_settings(client)
        finally:
            client.close()
        print(f"Isolated test app: {URL}\nTest context: {context_file}", flush=True)
        if args.prepare_only:
            return
        for journey in args.only:
            started = time.monotonic()
            print(f"RUN {journey}", flush=True)
            subprocess.run(
                [sys.executable, str(ROOT / "tests" / f"run_{journey}.py")], cwd=ROOT, env=env, check=True
            )
            print(f"PASS {journey} ({time.monotonic() - started:.1f}s)", flush=True)
    finally:
        if keep:
            print(
                f"Kept isolated test deployment. Cleanup: uv run python tests/run_e2e.py --context {home} --cleanup",
                flush=True,
            )
        else:
            cleanup()


if __name__ == "__main__":
    main()
