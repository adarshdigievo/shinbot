"""Deterministic OpenAI-compatible fixture. It never calls a paid model."""

import asyncio
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI()
requests = []
streamed = set()
browser_gates = {}


def browser_gate(marker, phase):
    return browser_gates.setdefault((marker, phase), asyncio.Event())


@app.post("/watch-fixture/{marker}/{phase}")
async def release_browser_fixture(marker: str, phase: str):
    browser_gate(marker, phase).set()
    return {"released": True}


@app.get("/watch-fixture/{marker}")
async def browser_watch_fixture(marker: str):
    async def content():
        yield """<!doctype html><html><head><title>Daily planner</title><style>
body{font:20px system-ui;background:#f7f8fa;color:#18243a;padding:60px;line-height:1.7}
main{max-width:720px;margin:auto}h1{font-size:42px;margin-bottom:12px}article{background:white;border:1px solid #e4e7ed;border-radius:12px;padding:24px;margin:18px 0}
small{color:#647083}input{font:inherit;padding:10px;width:90%}</style></head><body><main><small>YOUR WORKSPACE</small><h1>Plan a quieter week.</h1><p>Review appointments and leave room for focused work.</p><article><strong>Tuesday, 10:00 am</strong><p>Project check-in with the team</p></article><article><strong>Keep a note</strong><p><input placeholder="What needs your attention?" aria-label="Planner note"></p></article><p id="clock"></p><script>setInterval(()=>document.getElementById('clock').textContent=new Date().toLocaleTimeString(),1000)</script>"""
        await browser_gate(marker, "navigation").wait()
        yield "</main></body></html>"

    return StreamingResponse(content(), media_type="text/html")


@app.get("/fixture-metrics")
async def metrics():
    return {"model_requests": len(requests)}


@app.get("/website", response_class=HTMLResponse)
async def website():
    return '<!doctype html><html><head><title>ShinBot browser fixture</title></head><body><h1>A small daily routine</h1><p>Available appointments: Tuesday at 10 am.</p><form><label>Your note <input id="note" name="note"></label><button type="button" onclick="document.querySelector(\'#result\').textContent=document.querySelector(\'#note\').value">Save note</button></form><p id="result"></p></body></html>'


@app.get("/cookie", response_class=HTMLResponse)
async def cookie(request: Request, value: str | None = None):
    response = HTMLResponse(
        "<html><body>Cookie value: "
        + (value if value is not None else request.cookies.get("profile_test", "missing"))
        + "</body></html>"
    )
    if value is not None:
        response.set_cookie("profile_test", value, max_age=3600, httponly=True)
    return response


@app.post("/v1/chat/completions")
async def completion(request: Request):
    body = await request.json()
    requests.append(body)
    messages = body["messages"]
    user = next((m for m in reversed(messages) if m["role"] == "user"), {})
    content = user.get("content", "")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content)
    watch_marker = next(
        (
            match.group(1)
            for message in messages
            if message["role"] == "user"
            and isinstance(text := message.get("content"), str)
            and (match := re.fullmatch(r"fixture-watch:([a-f0-9]{32})", text))
        ),
        None,
    )
    if watch_marker and any(message["role"] == "tool" for message in messages):
        await browser_gate(watch_marker, "model").wait()
    if "FIXTURE AUTH ERROR" in content:
        return JSONResponse(
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                    "message": "Invalid API key PRIVATE-FIXTURE-API-KEY",
                }
            },
            status_code=401,
        )
    latest = messages[-1]
    tool = None
    pause_stream = False
    if latest["role"] != "tool":
        if match := re.fullmatch(r"fixture-project:([a-z0-9-]+):(\d+)", content):
            label, delay = match.groups()
            tool = (
                "run_command",
                {
                    "command": f"printf '{label}\\n' >> feature.txt; sleep {min(int(delay), 30)}; pwd",
                    "timeout": 60,
                },
            )
        elif match := re.fullmatch(r"fixture-delegate:([a-f0-9]{32}|default)", content):
            tool = (
                "plg__projects__delegate_task",
                {
                    "bot_id": match.group(1),
                    "title": "Review the feature",
                    "prompt": "fixture-project:reviewer:1",
                },
            )
        elif match := re.fullmatch(r"fixture-recovery(-stream)?:([a-f0-9]{32}):(\d+)", content):
            streaming, marker, delay = match.groups()
            pause_stream = bool(streaming and marker not in streamed)
            streamed.add(marker)
            tool = (
                "run_command",
                {
                    "command": f"printf 'effect\\n' >> recovery-{marker}.txt; sleep {min(int(delay), 120)}; printf 'recovered result'",
                    "timeout": 180,
                },
            )
        elif "fixture-board" in content:
            continuing = "Previous run: \n" in content
            tool = (
                "plg__kanban__finish_run",
                {
                    "outcome": "continue" if continuing else "completed",
                    "summary": "Checked the first acceptance criterion."
                    if continuing
                    else "Verified both acceptance criteria.",
                    "evidence": "Deterministic runtime fixture exercised the native Kanban tool.",
                    "next_step": "Verify the second acceptance criterion." if continuing else "",
                },
            )
        elif "fixture-cookie-set:" in content:
            from urllib.parse import quote

            tool = (
                "browser",
                {
                    "operation": "goto",
                    "url": os.getenv("SHINBOT_FIXTURE_ORIGIN", "http://fixture:8888")
                    + "/cookie?value="
                    + quote(content.split("fixture-cookie-set:", 1)[1].strip()),
                },
            )
        elif "fixture-cookie-read" in content:
            tool = (
                "browser",
                {
                    "operation": "goto",
                    "url": os.getenv("SHINBOT_FIXTURE_ORIGIN", "http://fixture:8888") + "/cookie",
                },
            )
        elif "fixture-write" in content:
            tool = ("write_file", {"path": "fixture-result.txt", "content": "Persistent fixture result"})
        elif "fixture-preference" in content:
            tool = ("memory", {"operation": "remember", "text": "Use metric units"})
        elif "fixture-note" in content:
            tool = (
                "notes",
                {"operation": "save", "title": "Fixture research", "body": "A useful saved result"},
            )
        elif "fixture-command" in content:
            tool = ("run_command", {"command": "printf 'approved command completed'", "timeout": 10})
        elif match := re.fullmatch(r"fixture-watch:([a-f0-9]{32})", content):
            tool = (
                "browser",
                {"operation": "goto", "url": "http://fixture:8888/watch-fixture/" + match.group(1)},
            )
        elif "fixture-browser" in content:
            tool = (
                "browser",
                {
                    "operation": "goto",
                    "url": os.getenv("SHINBOT_FIXTURE_ORIGIN", "http://fixture:8888") + "/website",
                },
            )
        elif "fixture-plugin" in content:
            offered = [
                t["function"]["name"]
                for t in body.get("tools", [])
                if t["function"]["name"].startswith("plg__personal")
            ]
            if offered:
                tool = (offered[0], {"text": "hello"})
        elif "fixture-wait" in content:
            tool = ("run_command", {"command": "sleep 30; printf 'should not finish'", "timeout": 60})
    last_tool_name = latest.get("name")
    if latest["role"] == "tool" and not last_tool_name:
        last_tool_name = next(
            (
                call.get("function", {}).get("name")
                for message in reversed(messages)
                for call in message.get("tool_calls", [])
                if call.get("id") == latest.get("tool_call_id")
            ),
            None,
        )
    attachment_turn = next(
        (
            i
            for i in reversed(range(len(messages)))
            if messages[i]["role"] == "user" and "fixture-chat-files" in str(messages[i].get("content", ""))
        ),
        None,
    )
    if attachment_turn is not None:
        completed = sum(message["role"] == "tool" for message in messages[attachment_turn + 1 :])
        steps = [
            ("write_file", {"path": "reports/week.csv", "content": "day,task\nTuesday,Project check-in\n"}),
            ("attach_file", {"path": "reports/week.csv", "name": "weekly-plan.csv"}),
            ("browser", {"operation": "goto", "url": os.getenv("SHINBOT_FIXTURE_ORIGIN") + "/website"}),
            ("browser", {"operation": "screenshot", "name": "appointments.png"}),
        ]
        tool = steps[completed] if completed < len(steps) else None
    if "fixture-learn" in content:
        if latest["role"] != "tool":
            tool = ("write_file", {"path": "learning-check.txt", "content": "Verified learning fixture"})
        elif last_tool_name == "write_file":
            tool = ("status", {})
        elif last_tool_name == "status":
            state = json.loads(latest["content"])
            key = next(e["id"] for e in state["recent_evidence"] if e["tool"] == "write_file")
            tool = (
                "plugins",
                {
                    "operation": "learn",
                    "name": "verify-file",
                    "description": "Write a workspace file and check its saved result.",
                    "instructions": "Write the file with the workspace tool, then read it back. Compare the saved text with the requested contents and report any differences before claiming success.",
                    "evidence": [key],
                },
            )
    if "fixture-budget" in content:
        turn_start = max(i for i, message in enumerate(messages) if message["role"] == "user")
        completed_tools = sum(message["role"] == "tool" for message in messages[turn_start + 1 :])
        tool = ("notes", {"operation": "search", "query": "budget fixture"}) if completed_tools < 2 else None
        if completed_tools == 1 and content.startswith("Work card "):
            continuing = "Previous run: \n" in content
            tool = (
                "plg__kanban__finish_run",
                {
                    "outcome": "continue" if continuing else "completed",
                    "summary": "Verified the first step." if continuing else "Verified both steps.",
                    "evidence": "Budget fixture completed its tool loop.",
                    "next_step": "Verify the second step." if continuing else "",
                },
            )
    text = "Fixture completed. " + (
        str(latest.get("content", ""))[:500] if latest["role"] == "tool" else str(content)[:500]
    )
    if attachment_turn is not None and tool is None:
        text = "Here is your weekly plan and a screenshot of the appointments page."
    if any(
        "Suggest one short, useful message" in str(m.get("content", ""))
        for m in messages
        if m["role"] in {"system", "developer"}
    ):
        text = "Compare the remaining options and recommend the best next step."
        tool = None
    finish_reason = "stop"
    if content == "fixture-empty-answer":
        text = ""
    elif content == "fixture-truncated-answer":
        text, finish_reason = "The first incomplete step", "length"
    if "fixture-memory-check" in content:
        text = (
            "Memory refreshed"
            if any(
                "Use metric units" in str(m.get("content", ""))
                for m in messages
                if m["role"] in {"system", "developer"}
            )
            else "Memory missing"
        )
    if "summar" in content.lower():
        text = "The user asked for a fixture task. Preserve its saved note and file, and the current task objective."
    if content.startswith("fixture-queue:"):
        marker = content.split(":", 1)[1]
        await browser_gate(marker, "reply").wait()
        tool = (
            ("notes", {"operation": "save", "title": "Steering checkpoint", "body": "Created once."})
            if marker == "first" and latest["role"] != "tool"
            else None
        )
        text = "Finished " + marker
    identifier = "chatcmpl-" + uuid.uuid4().hex

    def frame(delta, finish=None):
        return {
            "id": identifier,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "fixture",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    async def stream():
        yield "data: " + json.dumps(frame({"role": "assistant"})) + "\n\n"
        if tool:
            yield (
                "data: "
                + json.dumps(
                    frame(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_" + uuid.uuid4().hex,
                                    "type": "function",
                                    "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
                                }
                            ]
                        }
                    )
                )
                + "\n\n"
            )
            if pause_stream:
                await asyncio.sleep(10)
        else:
            for offset in range(0, len(text), 40):
                yield "data: " + json.dumps(frame({"content": text[offset : offset + 40]})) + "\n\n"
        final = frame({}, "tool_calls" if tool else finish_reason)
        final["usage"] = (
            {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}
            if "fixture-budget" in content
            else {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}
        )
        yield "data: " + json.dumps(final) + "\n\ndata: [DONE]\n\n"

    if body.get("stream"):
        return StreamingResponse(stream(), media_type="text/event-stream")
    return {
        "id": identifier,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "fixture",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
    }
