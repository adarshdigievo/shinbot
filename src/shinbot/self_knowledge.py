"""Product help loaded on demand by the agent."""

import json
import os
from pathlib import Path

from .bots import Profile, Template
from .plugins import PluginManifest


def help_topic(topic):
    topics = {
        "overview": {
            "product": "ShinBot is a self-hosted personal assistant, not a model provider.",
            "components": {
                "agent": "Published Pi Durable packages in a TypeScript runtime",
                "backend": "Python and SQLite",
                "computer": "Persistent workspace with Python, Node, Git and shell tools. An optional Linux desktop provides a terminal, file manager, browser windows and human takeover. Check status browser.desktop before using desktop tools. Desktop screenshot shares the entire screen in chat; observe images are for the model.",
                "attachments": "Create files with write_file or commands, then use attach_file to deliver them in chat. Use browser operation screenshot to share the current viewport. Images preview inline; other files download. Maximum 10 MB per file. Attachments survive restarts and require owner authentication.",
                "browser": "Persistent Camoufox, Chromium or agent-browser profiles with human takeover; Camoufox is the default for new installations",
                "notebook": "Shared notes and separate memories for each bot",
                "jobs": "Scheduled jobs admitted by the service",
                "plugins": "Versioned skills, MCP tools, sidebar apps, themes, UI patches, lifecycle hooks, channels and optional native toolkits",
            },
            "live_state": "Call status for this conversation's model, permissions, selected extensions and browser profile. Call bots list for current profiles. Do not infer enabled tools from this help page.",
            "limits": [
                "The services and host must be running for scheduled work.",
                "Computer paths belong to the server, not automatically the browser user's device.",
                "Projects and Kanban require explicit plugin installation and activation.",
                "Use tools and their results as evidence; never report a setup action as completed without executing it.",
            ],
        },
        "bots": {
            "workflow": "Use bots list to discover actual IDs, roles and browser sharing. Use save or install to create profiles after approval. A profile can inherit the installation model or choose its own provider and model. Existing conversations keep their profile snapshot. The owner can change their model and thinking effort from chat.",
            "sharing": {
                "private": "Only this bot's browser profile",
                "group": "One browser profile for this group",
                "shared": "The all-bot browser profile",
            },
            "boundaries": "Browser sharing controls cookies and session storage, not host-administrator access. The owner can take over any browser profile. Provider credentials, Computer files and notebook pages belong to the installation. Memories are separate per bot. A memory grant of false means confirmation is required to save, not that reading memory is disabled.",
            "delegation": "Projects adds delegate_task for coding work in separate worktrees after approval. Bots are profiles, not a general cross-bot messaging bus. Worktrees begin at committed HEAD; a reviewer needs the original worktree path or diff to inspect uncommitted changes.",
            "github": "With Projects enabled, list_inbox refreshes GitHub activity. Use view all to include closed issues and merged pull requests; these leave Needs attention. read_issue checks the current issue or pull request and its comments, including whether it was merged outside ShinBot. Report stale data or refresh warnings instead of claiming a verified current status.",
            "templates": "Use export for a portable bot/group profile and save_template for the local library. Credentials, memories and chats are excluded. Publishing or opening a PR needs an explicit owner request.",
            "profile_schema": Profile.model_json_schema(),
            "template_schema": Template.model_json_schema(),
        },
        "approvals": {
            "workflow": "The chat composer offers Auto, Ask, and Full access. Auto is the default: routine navigation and recognized search controls proceed; destructive or unclear actions require a separate Allow once or Decline decision. Browser checks use the actual target and are repeated before execution; changed targets need a new decision. Ask requests permission before commands and browser actions, honoring eligible saved rules. Full access runs actions without approval and is an explicit owner choice. Human browser takeover, plugin access, security scans and recovery holds still apply. Never evade a pending or denied decision by changing tools or arguments.",
            "settings": "status reports approval_mode, thinking_level and grants. Only the owner can change chat controls. The compact model picker lists connected models; effort choices follow upstream model capabilities. Model and effort changes apply to subsequent messages and wait until the active reply finishes. Approval mode can change while a chat waits. Settings holds provider connections, defaults and saved permissions. Auto ignores broad saved permissions for destructive or unclear actions. Delegated work inherits the stricter parent and installation approval mode; global saved rules do not elevate it.",
            "recovery": "Network waits resume automatically when an action can be retried safely. Completed action receipts prevent duplicate execution. An uncertain consequential action needs inspection and owner resolution before another attempt.",
        },
        "storage": {
            "host": "The launcher uses ~/.shinbot by default, or SHINBOT_HOME. A venv or source checkout does not determine the persistent data location.",
            "services": "Docker stores gateway records, runtime state, browser profiles and Computer files in named volumes. Computer files are under /workspace in the container. For direct service execution, SHINBOT_DATA controls gateway records only; SHINBOT_WORKSPACE controls Computer files, SHINBOT_RUNTIME_DATA controls runtime state, and SHINBOT_BROWSER_DATA controls browser profiles. These are separate locations.",
            "backup": "On the installation host: shinbot backup /private/path/backup.tar.gz. Backups include settings, credentials, plugins, browser sessions, notes, memories and workspace data. They are private but not encrypted.",
            "rollback": "shinbot rollback restores the previous service release and its checkpoint data. Recovery holds automation for owner review. Do not delete volumes or use pip uninstall as a data reset.",
        },
        "updates": {
            "discovery": "Use updates check to query official GitHub and PyPI release metadata. updates status reads the last check. Settings > Updates shows the installed version, latest verified stable version and release notes. Automatic checks run daily unless disabled.",
            "apply": "The owner runs shinbot update --latest on the installation host. It verifies the stable release, upgrades a supported launcher installation and applies service images with a backup and health checks. Failure restores the previous service release and checkpoint data. Source checkouts use their existing build workflow.",
            "boundary": "The model and gateway cannot update the host through a chat tool. Do not run the host update command in Computer; it is a different environment. No release availability claim is valid when the check reports unavailable, unpublished or still publishing.",
        },
        "plugins": {
            "workflow": [
                "Search configured marketplaces first with plugins search.",
                "Choose a skill for instructions, an MCP plugin for executable tools, or kind extension for sidebar pages, themes, UI patches and lifecycle hooks. Extensions can also declare MCP tools. Read the manifest schema and examples below.",
                "Write files with relative paths such as plugins/example/server.py, never /workspace-prefixed paths. Test executable code in Computer with approved commands. Use only installed libraries or request a host-reviewed dependency setup.",
                'After writing and testing a package, call plugins with {"operation":"stage","path":"plugins/example"}. This reads shinbot-plugin.json and its declared files from the Computer directory. It validates and saves them without execution or activation. You can alternatively pass files mapping package-relative filenames to complete UTF-8 CONTENTS; use one format, not both.',
                "Check the stage result security report: high or critical findings, scan errors and missing scans block activation even with automatic approval. A staged package is not active. Use plugins search to verify its record. A failed tool result means nothing was staged; correct it before claiming success.",
                "Review files and request activation. New conversations select the activated version; the authoring conversation does not gain new tools mid-run.",
                "New agent-created packages belong to the creating bot. Use set_access after approval to share them. Preserve the upstream license when importing.",
            ],
            "security": "Plugins, skills and saved/imported templates are checked with a pinned local Cisco Skill Scanner static scan. Scans do not prove safety. MCP scans cover supplied settings and tool descriptions, not remote code or packages downloaded by launch commands.",
            "learning": "After successful work, plugins learn can stage a reusable skill using completed tool action IDs from status recent_evidence. Personal preferences belong in memory. Do not call a proposed or staged skill active. Respect the learning setting and do not record secrets or private task transcripts.",
            "limits": "Packages are immutable per ID/version, at most 2 MB and 101 files. No absolute, hidden or parent-traversal paths. files lists every package file except shinbot-plugin.json. Additional requires dependencies need a host-reviewed runner. builtin is reserved for shipped native toolkits; custom sidebar apps use kind extension and ui.pages. Hooks run in Computer; plugins do not patch Python gateway source. An MCP tool's input_schema must match its Python signature.",
            "ui": {
                "manifest": {
                    "kind": "extension",
                    "permissions": ["ui.pages", "ui.styles", "ui.patch"],
                    "ui": {
                        "pages": [
                            {"id": "paint", "title": "Paint", "icon": "pencil", "entrypoint": "paint.js"}
                        ],
                        "styles": ["theme.css"],
                        "patches": ["toolbar.js"],
                        "assets": [],
                    },
                },
                "contract": "A page module exports mount(root, sdk); a patch exports activate(sdk). Both may return cleanup functions. Use sdk.signal to cancel work on navigation or disable. sdk.storage.get/set/delete persists JSON per plugin and bot across updates. sdk.asset(path) addresses declared assets. sdk.api(path, method, body) uses authenticated ShinBot APIs. sdk.compose(text) opens an unsent chat draft. sdk.navigate(view), sdk.toast(text) and sdk.on(navigate|snapshot|appearance, handler) are available. sdk.mount(slot, element) inserts persistent UI in composer-actions, sidebar-footer or topbar; its cleanup is automatic. Helpers and images must be declared in ui.assets and files. Use existing Lucide icons and theme CSS variables.",
                "trust": "Activated UI modules run with the signed-in app's access. Review code before activation. Styles and pages load for allowed bots only. Disable removes contributions; ?extensions=off skips all plugin UI for recovery. No example is installed by default.",
            },
            "hooks": {
                "manifest": {
                    "permissions": ["hooks"],
                    "hooks": [{"event": "before_chat", "entrypoint": "hooks.py", "timeout": 10}],
                },
                "contract": "Export synchronous handle(event, context) returning a JSON object. context includes event_id, task_id, bot_id, data and a persistent Computer data_dir for the plugin. before_chat receives content and context; return context text to add model instructions, or block text. before_tool receives tool and args; return args to replace arguments before approvals, or block text. after_tool receives tool, args and result after a completed tool; optionally return result to transform what the model receives. after_response receives answer, status and error; return {}. Keep after_response side effects idempotent using event_id. Each event is version-pinned to the conversation, recorded durably and ordered by plugin ID. Uncertain hooks are not replayed automatically. Hooks run with Computer file/network permissions; they cannot approve an action. Set timeout from 1 to 60 seconds.",
            },
            "manifest_schema": PluginManifest.model_json_schema(),
        },
    }
    if topic not in topics:
        raise ValueError("Choose overview, bots, plugins, approvals, storage or updates.")
    result = topics[topic]
    if os.getenv("SHINBOT_NATIVE_EXECUTION") == "1" or os.getenv("SHINBOT_INSTALLATION") == "native":
        if topic == "storage":
            result.update(
                services="This native installation stores its five service data directories under ~/.shinbot/native, outside the Python environment. Computer runs with the owner's macOS account permissions. Use status for the actual workspace and gateway paths.",
                backup="Stop the native installation with shinbot native stop, then copy its entire private data directory to a private backup location before running shinbot native start. The copy includes credentials and must be protected. The Docker backup command does not back up native data.",
                rollback="Keep a stopped copy of the native data and the previous Python/runtime installation before changing versions. The Docker rollback command applies only to the separate Docker installation.",
            )
        elif topic == "updates":
            result["apply"] = (
                "This native source installation is updated by its owner: stop it with shinbot native stop, back up the native data, update the Python package and built Pi runtime, then run shinbot native start and shinbot native status. Docker image update commands do not update this native installation."
            )
        if os.getenv("SHINBOT_DESKTOP") == "1":
            if topic == "storage":
                result["services"] = (
                    "The native Python gateway and Pi runtime run on the Mac. Computer and browser run in the Apple Container Linux desktop. The workspace is shared from ~/.shinbot/native/workspace; Linux service data lives in the persistent desktop volume."
                )
                result["backup"] = (
                    "Run shinbot native desktop-backup while tasks are idle. It stops services, copies native settings, workspace and desktop volume data into the native backups directory, and restarts services. Protect the backup because it includes credentials."
                )
            elif topic == "updates":
                result["apply"] = (
                    "Run shinbot native desktop-backup, then shinbot native stop before updating the native Python package, Pi runtime and desktop image. Restart with shinbot native start and check shinbot native status."
                )
    if topic == "plugins":
        manifest = {
            "schema_version": 1,
            "id": "text-helper",
            "name": "Text helper",
            "version": "1.0.0",
            "description": "Count words in supplied text.",
            "kind": "mcp",
            "license": "MIT",
            "files": ["LICENSE", "server.py"],
            "permissions": ["computer"],
            "entrypoint": "server.py",
            "tools": [
                {
                    "name": "count_words",
                    "description": "Count words in supplied text.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ],
        }
        result["example_files"] = {
            "shinbot-plugin.json": json.dumps(manifest, indent=2),
            "LICENSE": (Path(__file__).parent / "bundled/official/plugins/projects/LICENSE").read_text(),
            "server.py": 'from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("Text helper")\n@mcp.tool()\ndef count_words(text: str) -> dict:\n    return {"words": len(text.split())}\nif __name__ == "__main__":\n    mcp.run()\n',
        }
        result["example_stage_call"] = {"operation": "stage", "path": "plugins/text-helper"}
        result["skill_example"] = {
            "kind": "skill",
            "files": ["LICENSE", "SKILL.md"],
            "SKILL.md": "---\nname: helpful-procedure\ndescription: Explain when to use this procedure.\n---\nDescribe tested steps and how to verify their result.\n",
            "note": "Use the same required identity, version, description and license fields; omit entrypoint and tools.",
        }
    return result
