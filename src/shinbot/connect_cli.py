"""Local sign-in helpers; credentials travel only to the selected ShinBot server."""

import http.server
import queue
import shutil
import subprocess
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
import typer


def server_url(value):
    parsed = urlparse(value)
    if (
        not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
        or not (parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ))
    ):
        raise typer.BadParameter("Use your ShinBot HTTPS address or a localhost address.")
    return value.rstrip("/")


def bridge_request(url, path, code, payload=None):
    try:
        response = httpx.post(
            server_url(url) + path, headers={"Authorization": "Bearer " + code},
            json=payload or {}, timeout=35, follow_redirects=False, trust_env=False,
        )
        if not response.is_success:
            raise ValueError("The connection could not finish. Check the request in ShinBot and try again.")
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise typer.BadParameter(
            "Could not connect. Check the ShinBot address and start a fresh connection request."
        ) from None


def connect_github(url, code):
    url = server_url(url)
    if not code or len(code) > 100:
        raise typer.BadParameter("Start Connect GitHub in the app and copy its command.")
    executable = shutil.which("gh")
    if not executable:
        raise typer.BadParameter("Install GitHub CLI from https://cli.github.com, then run this command again.")
    typer.echo(f"Connecting GitHub to {url}. You will choose repositories in ShinBot.")

    def credential():
        try:
            return subprocess.run(
                [executable, "auth", "token", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise typer.BadParameter("GitHub CLI could not read your account. Run gh auth status.") from None

    result = credential()
    if result.returncode or not result.stdout.strip():
        try:
            login = subprocess.run(
                [executable, "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"],
                timeout=540, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise typer.BadParameter("GitHub sign-in did not finish. Start again in ShinBot.") from None
        if login.returncode:
            raise typer.BadParameter("GitHub sign-in was cancelled. Start again when ready.")
        result = credential()
    if result.returncode or not result.stdout.strip():
        raise typer.BadParameter("GitHub CLI has no saved credential. Run gh auth login, then try again.")
    account = bridge_request(url, "/auth/github/pair", code, {"token": result.stdout.strip()})
    typer.echo(f"Connected as @{account['login']}. Return to ShinBot to choose repositories.")


def connect_chatgpt(url, code):
    url = server_url(url)
    if not code or len(code) > 100:
        raise typer.BadParameter("Start Continue with ChatGPT in ShinBot and copy its command.")
    received = queue.Queue(maxsize=1)
    expected_state = None
    accepted = False

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/done":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(b"<!doctype html><title>ShinBot sign-in</title><h1>Return to ShinBot</h1><p>Your callback was received. ShinBot is checking the connection.</p>")
                return
            state = parse_qs(parsed.query).get("state", [])
            if parsed.path != "/auth/callback" or not expected_state or state != [expected_state] or len(self.path) > 20000:
                self.send_error(400, "No matching sign-in")
                return
            try:
                received.put_nowait(parsed.query)
            except queue.Full:
                self.send_error(409, "Callback already received")
                return
            self.send_response(303)
            self.send_header("Location", "/done")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        bridge_request(url, "/auth/chatgpt-bridge/start", code, {"redirect_uri": redirect})
        deadline = time.monotonic() + 540
        opened = False
        typer.echo(f"Connecting ChatGPT to {url}. Complete sign-in in your browser.")
        while time.monotonic() < deadline:
            status = bridge_request(url, "/auth/chatgpt-bridge/status", code)
            if status["status"] == "complete":
                accepted = True
                typer.echo("ChatGPT connected. Return to ShinBot.")
                return
            if status["status"] != "pending":
                raise typer.BadParameter("Sign-in did not finish. Return to ShinBot to see the result.")
            if not opened:
                for event in status.get("events", []):
                    authorize = event.get("url", "")
                    parsed = urlparse(authorize)
                    if parsed.scheme == "https" and parsed.netloc == "auth.openai.com" and parsed.path == "/api/accounts/authorize":
                        params = parse_qs(parsed.query)
                        if params.get("redirect_uri") != [redirect] or not params.get("state"):
                            raise typer.BadParameter("The sign-in address does not match this helper.")
                        expected_state = params["state"][0]
                        opened = True
                        if not webbrowser.open(authorize):
                            typer.echo("Open this sign-in link in your browser:\n" + authorize)
                        break
            try:
                query = received.get(timeout=1)
                bridge_request(url, "/auth/chatgpt-bridge/callback", code, {"query": query})
            except queue.Empty:
                pass
        raise typer.BadParameter("Sign-in expired. Start again in ShinBot.")
    finally:
        server.shutdown()
        server.server_close()
        if not accepted:
            try:
                bridge_request(url, "/auth/chatgpt-bridge/cancel", code)
            except (typer.BadParameter, ValueError):
                pass
