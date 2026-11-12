"""Real browser service checks. Install all optional backends before running."""

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "test-results/browser-backends"


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"""<title>Backend test</title><h1>Browser fixture</h1>
        <input id="text"><button id="save">Save</button><output id="result"></output>
        <script>const out=document.querySelector('#result'),input=document.querySelector('#text');
        function render(){out.textContent='Saved '+(localStorage.count||0)+' '+(localStorage.text||'');}
        document.querySelector('#save').onclick=()=>{localStorage.count=Number(localStorage.count||0)+1;localStorage.text=input.value;render()};
        render();</script>""")

    def log_message(self, *args):
        pass


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{fixture.server_port}/"
    try:
        with tempfile.TemporaryDirectory(
            prefix="sb-backends-", dir="/private/tmp" if sys.platform == "darwin" else None
        ) as temporary:
            root = Path(temporary)
            token = secrets.token_urlsafe(32)
            env = {k: v for k, v in os.environ.items() if not k.startswith("SHINBOT_")}
            env.update(
                SHINBOT_EXECUTOR_TOKEN=token, SHINBOT_BROWSER_DATA=str(root), SHINBOT_BROWSER_VIEW="native"
            )
            for backend in sys.argv[1:] or ["chromium", "agent-browser", "camoufox"]:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                with (OUT / f"{backend}.log").open("w") as log:

                    def start():
                        return subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "uvicorn",
                                "shinbot.services.browser:app",
                                "--host",
                                "127.0.0.1",
                                "--port",
                                str(port),
                                "--no-access-log",
                            ],
                            env=env,
                            stdout=log,
                            stderr=log,
                        )

                    process = start()
                    client = httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": "Bearer " + token},
                        timeout=65,
                        trust_env=False,
                    )

                    def ready():
                        for _ in range(150):
                            try:
                                if client.get("/health").is_success:
                                    return
                            except httpx.HTTPError:
                                pass
                            time.sleep(0.1)
                        raise AssertionError(f"{backend} did not start")

                    def post(path, payload):
                        response = client.post(path, json={"backend": backend, **payload})
                        response.raise_for_status()
                        return response.json()

                    def action(key, operation, profile="shared", **args):
                        return post(
                            "/execute",
                            {
                                "id": backend + key,
                                "epoch": "test",
                                "task_id": "test",
                                "browser_id": profile,
                                "approval_mode": "full",
                                "args": {"operation": operation, **args},
                            },
                        )

                    def text(result):
                        return " ".join(c.get("text", "") for c in result.get("content", []))

                    try:
                        ready()
                        assert next(
                            b for b in client.get("/backends").json()["backends"] if b["id"] == backend
                        )["ready"]
                        assert "Saved 0" in text(action("-open", "goto", url=url))
                        # Typed input must not become CLI flags, shell commands, or a different session.
                        sample = '--session other "quoted" $(echo nope)'
                        action("-fill", "fill", selector="#text", text=sample)
                        result = action("-click", "click", selector="#save")
                        assert "Saved 1 " + sample in text(result), result
                        assert action("-click", "click", selector="#save") == result
                        frame = client.get("/view").json()
                        pixels = base64.b64decode(frame["screenshot"])
                        assert pixels.startswith(b"\xff\xd8") and len(pixels) > 1000
                        (OUT / f"{backend}.jpg").write_bytes(pixels)
                        action("-next", "goto", url=url + "next")
                        action("-back", "back")
                        assert client.get("/state").json()["url"] == url
                        # Completion must not close a browser taken over by its owner.
                        post("/takeover", {"owner": "human"})
                        post("/agent/release", {"task_id": "test", "close": True})
                        assert not client.get("/lease").json()["idle"]
                        post("/release", {})
                        action("-observe", "observe")
                        assert post("/agent/release", {"task_id": "test", "close": True})["closed"]
                        assert client.get("/view").json()["empty"]
                        # Persisted state and receipts survive a real service process restart.
                        process.terminate()
                        process.wait(timeout=20)
                        process = start()
                        ready()
                        from shinbot.results import without_images

                        assert action("-click", "click", selector="#save") == without_images(result)
                        assert client.get("/lease").json()["idle"]
                        assert "Saved 1 " + sample in text(action("-reopen", "goto", url=url))
                        action("-close", "close")
                        assert "Saved 0" in text(action("-private", "goto", profile="private-bot", url=url))
                        action("-close-private", "close", profile="private-bot")
                        assert "Saved 1 " + sample in text(action("-shared", "goto", url=url))
                        action("-final-close", "close")
                        print(
                            json.dumps(
                                {
                                    "backend": backend,
                                    "navigation": "passed",
                                    "actions_and_replay": "passed",
                                    "preview": "passed",
                                    "ownership": "passed",
                                    "restart_and_profiles": "passed",
                                }
                            ),
                            flush=True,
                        )
                    finally:
                        client.close()
                        process.terminate()
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
    finally:
        fixture.shutdown()
        fixture.server_close()


if __name__ == "__main__":
    main()
