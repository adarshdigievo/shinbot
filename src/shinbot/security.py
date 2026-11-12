"""Local extension scans bound to the exact artifact and scanner policy."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

from .db import packed

SCANNER_VERSION = "2.3.1"
POLICY = "static-high-v1"
SCAN_TIMEOUT = 50
_slots = threading.BoundedSemaphore(2)


def content_digest(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


class Security:
    def __init__(self, db):
        self.db = db

    def key(self, digest):
        return f"security-scan:{SCANNER_VERSION}:{POLICY}:{digest}"

    def report(self, digest):
        return self.db.setting(self.key(digest))

    def require(self, files):
        report = self.scan(files)
        if report["status"] != "passed":
            raise ValueError(
                report["message"]
                + " "
                + "; ".join(f["title"] for f in report["findings"] if f["severity"] in {"HIGH", "CRITICAL"})[
                    :600
                ]
            )
        return report

    def scan(self, files, *, refresh=False):
        digest = content_digest(files)
        cached = self.report(digest)
        if not refresh and cached and cached["status"] in {"passed", "blocked"}:
            return cached
        with _slots:
            result = self._run(files)
        report = {
            **result,
            "digest": digest,
            "scanner": "Cisco Skill Scanner",
            "version": SCANNER_VERSION,
            "policy": POLICY,
            "scanned_at": time.time(),
        }
        self.db.set(self.key(digest), report)
        return report

    def _run(self, files):
        try:
            with tempfile.TemporaryDirectory(prefix="shinbot-scan-") as temp:
                root = Path(temp)
                package = root / "package"
                package.mkdir()
                for name, content in files.items():
                    path = PurePosixPath(name)
                    if (
                        path.is_absolute()
                        or any(part in {"", ".", ".."} for part in name.split("/"))
                        or "\\" in name
                    ):
                        raise ValueError("Invalid scan path.")
                    target = package / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                if "SKILL.md" not in files:
                    # Scan a copy. The original plugin/template remains byte-for-byte unchanged.
                    text = "---\nname: shinbot-extension\ndescription: Extension submitted for security review.\nlicense: MIT\n---\nReview the included extension files.\n"

                    def strings(value):
                        if isinstance(value, str):
                            return [value]
                        if isinstance(value, dict):
                            return [text for item in value.values() for text in strings(item)]
                        if isinstance(value, list):
                            return [text for item in value for text in strings(item)]
                        return []

                    for name, content in files.items():
                        if name.endswith(".json"):
                            text += "\n\n" + "\n\n".join(strings(json.loads(content)))
                        elif name.endswith(".md"):
                            text += "\n\n" + content.decode("utf-8", errors="replace")
                    (package / "SKILL.md").write_text(text)
                environment = {
                    "PATH": os.defpath,
                    "HOME": temp,
                    "LANG": "C.UTF-8",
                    "TMPDIR": temp,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "HF_HUB_OFFLINE": "1",
                }
                with (root / "report.json").open("w+b") as output, (root / "errors").open("w+b") as errors:
                    process = subprocess.Popen(
                        [sys.executable, "-I", "-m", "shinbot.scan_worker", str(package)],
                        cwd=temp,
                        env=environment,
                        stdout=output,
                        stderr=errors,
                        start_new_session=True,
                    )
                    try:
                        code = process.wait(timeout=SCAN_TIMEOUT)
                    finally:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                    output.seek(0)
                    if code != 0:
                        raise ValueError("Scanner process failed.")
                    payload = json.loads(output.read(8_000_001))
                return scan_verdict(payload)

        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,
            subprocess.TimeoutExpired,
        ):
            return {
                "status": "error",
                "findings": [],
                "message": "Security scan could not finish. Activation is blocked. Retry the scan; for a source installation, run uv sync --extra security. For a pip backend, install shinbot[security] in its environment.",
            }

    def template(self, value):
        return self.require({"template.json": packed(value).encode()})


def scan_verdict(payload):
    if payload.get("version") != SCANNER_VERSION:
        raise ValueError("Scanner version does not match the pinned version.")
    data = payload["result"]
    if not isinstance(data.get("findings"), list) or any(
        not isinstance(f, dict)
        or f.get("severity") not in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "SAFE"}
        for f in data["findings"]
    ):
        raise ValueError("Security report has an unknown finding format.")
    if data.get("analyzers_failed") or "static_analyzer" not in data.get("analyzers_used", []):
        raise ValueError("Security analysis was incomplete.")
    findings = [
        {
            key: item.get(key)
            for key in (
                "severity",
                "title",
                "description",
                "file_path",
                "line_number",
                "remediation",
            )
        }
        for item in data["findings"]
    ]
    blocked = any(str(f["severity"]).upper() in {"HIGH", "CRITICAL"} for f in findings)
    return {
        "status": "blocked" if blocked else "passed",
        "findings": findings,
        "message": "Security scan blocked this extension. Review the high or critical findings in Plugins."
        if blocked
        else "Local scan completed. Review any findings before activation; a scan cannot prove code is safe.",
    }
