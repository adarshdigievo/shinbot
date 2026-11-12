"""Git workspace operations, executed by the unprivileged Computer worker."""

import fcntl
import os
import re
import subprocess
from pathlib import Path

from .common import workspace_path


def git(directory, *arguments, timeout=30):
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "status.showUntrackedFiles=all",
            "-c",
            "core.quotePath=false",
            "-C",
            str(directory),
            *arguments,
        ],
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()[:1000]
        raise ValueError(detail or "Git could not complete this operation.")
    return result.stdout.decode(errors="replace")


def repository(root, name):
    directory = workspace_path(root, name)
    if not directory.is_dir():
        raise ValueError("Folder not found in the Computer workspace.")
    top = Path(git(directory, "rev-parse", "--show-toplevel").strip()).resolve()
    common = Path(git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
    if top != directory or not common.is_relative_to(root.resolve()):
        raise ValueError("Choose the root of a Git repository stored inside the Computer workspace.")
    return directory


def workspace_directory(root, project_id, workspace_id):
    if any(
        not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key)
        for key in (project_id, workspace_id)
    ):
        raise ValueError("Invalid project or workspace ID.")
    return workspace_path(root, f".shinbot-worktrees/{project_id}/{workspace_id}")


def operate(root, args):
    root = root.resolve()
    operation = args["operation"]
    if operation == "browse":
        directory = workspace_path(root, args.get("path", "."))
        if not directory.is_dir():
            raise ValueError("Folder not found in the Computer workspace.")
        folders = [
            {"name": p.name, "path": str(p.relative_to(root)), "repository": (p / ".git").exists()}
            for p in sorted(directory.iterdir())
            if not p.name.startswith(".") and p.is_dir() and not p.is_symlink()
        ][:200]
        return {
            "path": str(directory.relative_to(root)),
            "folders": folders,
            "repository": (directory / ".git").exists(),
        }
    directory = repository(root, args["path"])
    if operation == "inspect":
        if directory.is_relative_to(root / ".shinbot-worktrees"):
            raise ValueError("Choose the original repository, not a managed task worktree.")
        return {
            "path": str(directory.relative_to(root)),
            "name": directory.name,
            "branch": git(directory, "branch", "--show-current").strip(),
            "commit": git(directory, "rev-parse", "--verify", "HEAD^{commit}").strip(),
            "dirty": bool(git(directory, "status", "--porcelain")),
        }
    workspace = workspace_directory(root, args["project_id"], args["workspace_id"])
    branch = "shinbot/" + args["workspace_id"]
    if operation in {"create", "archive"}:
        parent = workspace.parent
        parent.mkdir(parents=True, exist_ok=True)
        with (parent / "operations.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            archived = parent / (args["workspace_id"] + ".archived")
            prepared = parent / (args["workspace_id"] + ".ready")
            if operation == "archive":
                if workspace.exists():
                    repository(root, str(workspace.relative_to(root)))
                    if git(workspace, "branch", "--show-current").strip() != branch:
                        raise ValueError("Workspace branch changed. Inspect it before archiving.")
                    flags = git(workspace, "ls-files", "-v", "-z").split("\0")
                    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in flags):
                        raise ValueError(
                            "This worktree has files hidden from Git status. Clear assume-unchanged or skip-worktree flags before archiving."
                        )
                    # Include ignored files so local environments and build outputs are never silently removed.
                    if git(workspace, "status", "--porcelain", "--untracked-files=all", "--ignored"):
                        raise ValueError(
                            "This worktree has changed, untracked or ignored files. Save or remove them before archiving."
                        )
                    git(directory, "worktree", "remove", "--", str(workspace))
                archived.touch()
                return {"archived": True, "branch": branch}
            if archived.exists():
                raise ValueError("This worktree was archived. Start a new task from Projects.")
            commit = args["base_commit"]
            if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
                raise ValueError("Choose a resolved Git commit for the worktree.")
            if workspace.exists():
                repository(root, str(workspace.relative_to(root)))
                if git(workspace, "branch", "--show-current").strip() != branch:
                    raise ValueError("Workspace path is already used by another branch.")
            else:
                refs = git(
                    directory, "for-each-ref", "--format=%(objectname)", "refs/heads/" + branch
                ).strip()
                if refs:
                    if refs != commit:
                        raise ValueError("The saved workspace branch changed. Inspect it before retrying.")
                    git(directory, "worktree", "add", "--", str(workspace), branch, timeout=300)
                else:
                    git(directory, "worktree", "add", "-b", branch, "--", str(workspace), commit, timeout=300)
            if not prepared.exists():
                if git(workspace, "rev-parse", "HEAD").strip() != commit or git(
                    workspace, "status", "--porcelain", "--untracked-files=all"
                ):
                    raise ValueError(
                        "Worktree checkout did not finish cleanly. Inspect its files in Computer before removing or repairing it; no bot has been started."
                    )
                with prepared.open("w") as marker:
                    marker.write(commit)
                    marker.flush()
                    os.fsync(marker.fileno())
            elif prepared.read_text() != commit:
                raise ValueError("Worktree setup metadata changed. Inspect it before retrying.")
            return {"path": str(workspace.relative_to(root)), "branch": branch, "base_commit": commit}
    if not workspace.is_dir():
        raise ValueError("This worktree is missing. Check the Computer volume or restore a backup.")
    repository(root, str(workspace.relative_to(root)))
    if operation == "changes":
        commit = args["base_commit"]
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
            raise ValueError("Invalid base commit.")
        return {
            "status": git(workspace, "status", "--short")[:30000],
            "diff": git(workspace, "diff", "--no-ext-diff", "--no-textconv", "--stat", commit)[:30000],
            "patch": git(workspace, "diff", "--no-ext-diff", "--no-textconv", commit)[:150000],
            "branch": git(workspace, "branch", "--show-current").strip(),
        }
    if operation in {"files", "read"}:
        from .worker import file_operation

        name = args.get("file", ".")
        if ".git" in Path(name).parts:
            raise ValueError("Git internals are not shown in the file browser.")
        return file_operation(
            workspace, "list_files" if operation == "files" else "read_file", {"path": name}
        )
    raise ValueError("Unknown project operation.")
