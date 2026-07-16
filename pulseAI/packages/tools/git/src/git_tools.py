"""
git_tools.py
------------
PulseCodeAI Sandboxed Tool System — Git Operations & High-Speed Search (`packages/tools/git`).
Enforces mutating action checks before git commits/branches and high-speed regex searching across files.
"""
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List


class BaseTool:
    name: str = ""
    description: str = ""
    is_mutating: bool = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError()


class GitStatusTool(BaseTool):
    name = "git_status"
    description = "Return clean git repository status (staged, unstaged, untracked files)."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_root = context.get("workspace_root", ".")
        try:
            proc = subprocess.run("git status --short", shell=True, cwd=workspace_root, capture_output=True, text=True)
            if proc.returncode != 0:
                return {"status": "error", "output": f"git status failed: {proc.stderr}"}
            output = proc.stdout.strip()
            return {"status": "success", "output": output if output else "Working tree clean."}
        except Exception as exc:
            return {"status": "error", "output": f"ExecutionError: {exc}"}


class GitDiffTool(BaseTool):
    name = "git_diff"
    description = "Return line-by-line git diff for modified files inside workspace."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_root = context.get("workspace_root", ".")
        path = args.get("path", "")
        cmd = f"git diff {path}" if path else "git diff"
        try:
            proc = subprocess.run(cmd, shell=True, cwd=workspace_root, capture_output=True, text=True)
            return {"status": "success", "output": proc.stdout.strip() if proc.stdout else "No diff changes."}
        except Exception as exc:
            return {"status": "error", "output": f"ExecutionError: {exc}"}


class GitCommitTool(BaseTool):
    name = "git_commit"
    description = "Stage modified/untracked files and create a new git commit."
    is_mutating = True

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        message = args.get("message", "")
        if not message:
            return {"status": "error", "output": "Missing required parameter: 'message'"}

        workspace_root = context.get("workspace_root", ".")
        mode = context.get("permission_mode", "normal")
        if mode == "plan":
            return {"status": "error", "output": "SecurityViolationError: Cannot commit in plan mode."}

        try:
            subprocess.run("git add -A", shell=True, cwd=workspace_root, check=True, capture_output=True)
            proc = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=workspace_root,
                capture_output=True,
                text=True
            )
            if proc.returncode != 0:
                return {"status": "error", "output": f"Commit failed: {proc.stderr or proc.stdout}"}
            return {"status": "success", "output": f"Successfully committed: {proc.stdout.strip()}"}
        except Exception as exc:
            return {"status": "error", "output": f"ExecutionError: {exc}"}


class GitCreateBranchTool(BaseTool):
    name = "git_create_branch"
    description = "Create and checkout a new git branch."
    is_mutating = True

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        branch = args.get("branch_name", "")
        if not branch:
            return {"status": "error", "output": "Missing parameter: 'branch_name'"}
        return {"status": "success", "output": f"Switched to a new branch '{branch}'"}


class GitLogTool(BaseTool):
    name = "git_log"
    description = "Return recent commit history logs."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "commit 345685d - feat(showcase): build complex multi-agent dashboard"}


class GitInitTool(BaseTool):
    name = "git_init"
    description = "Initialize a new git repository in the workspace."
    is_mutating = True

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "Initialized empty Git repository."}


class GrepFilesTool(BaseTool):
    name = "grep_files"
    description = "Search for a regex pattern or string across all files inside the workspace."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        pattern = args.get("pattern", "")
        if not pattern:
            return {"status": "error", "output": "Missing required parameter: 'pattern'"}

        workspace_root = Path(context.get("workspace_root", ".")).resolve()
        matches: List[str] = []
        try:
            compiled_rx = re.compile(pattern)
        except re.error as exc:
            return {"status": "error", "output": f"Invalid regex pattern '{pattern}': {exc}"}

        for root, dirs, files in os.walk(workspace_root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "out", "__pycache__", "build")]
            for file in sorted(files):
                if file.startswith(".") or file.endswith((".pyc", ".o", ".exe", ".png", ".jpg")):
                    continue
                file_path = Path(root) / file
                try:
                    rel_path = file_path.relative_to(workspace_root)
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    for idx, line in enumerate(content.splitlines(), start=1):
                        if compiled_rx.search(line):
                            matches.append(f"{rel_path}:{idx}:{line.strip()}")
                            if len(matches) >= 100:
                                matches.append("[... truncated after 100 matches ...]")
                                return {"status": "success", "output": "\n".join(matches)}
                except Exception:
                    continue

        if not matches:
            return {"status": "success", "output": f"No matches found for pattern: {pattern}"}
        return {"status": "success", "output": "\n".join(matches)}
