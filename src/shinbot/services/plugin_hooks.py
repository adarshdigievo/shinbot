"""Lifecycle handlers executed under the Computer worker identity."""

import contextlib
import os
import runpy
import sys
from pathlib import Path


def run(payload):
    directory = Path(payload["directory"])
    state = Path.cwd() / ".shinbot" / "plugins" / payload["plugin_id"]
    state.mkdir(parents=True, exist_ok=True)
    os.environ.update(payload.get("environment", {}))
    context = {**payload["context"], "data_dir": str(state)}
    with contextlib.redirect_stdout(sys.stderr):
        module = runpy.run_path(str(directory / payload["entrypoint"]))
        handler = module.get("handle")
        if not callable(handler):
            raise ValueError("A plugin hook must export handle(event, context).")
        result = handler(payload["event"], context)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError("A plugin hook must return a JSON object or None.")
    return result
