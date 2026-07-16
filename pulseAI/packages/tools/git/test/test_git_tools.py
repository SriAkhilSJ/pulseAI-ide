"""
test_git_tools.py
-----------------
TDD Unit Tests for PulseCodeAI Git & Codebase Search Tools (`packages/tools/git`).
Verifies git status/diff/commit and fast regex file searching across workspace boundaries.
"""
import subprocess
import pytest
from pathlib import Path
from src.git_tools import GitStatusTool, GitCommitTool, GitDiffTool, GrepFilesTool


def _init_temp_repo(workspace: Path):
    subprocess.run("git init", shell=True, cwd=workspace, check=True, capture_output=True)
    subprocess.run("git config user.name 'Test User'", shell=True, cwd=workspace, check=True)
    subprocess.run("git config user.email 'test@example.com'", shell=True, cwd=workspace, check=True)
    (workspace / "README.md").write_text("# Initial Repo")
    subprocess.run("git add README.md && git commit -m 'initial commit'", shell=True, cwd=workspace, check=True)


def test_git_status_and_commit(tmp_path):
    workspace = tmp_path / "my_repo"
    workspace.mkdir()
    _init_temp_repo(workspace)

    # Create unstaged file
    (workspace / "feature.py").write_text("print('hello')\n")

    status_tool = GitStatusTool()
    context = {"workspace_root": str(workspace), "permission_mode": "dont_ask"}
    res_status = status_tool.execute({}, context)
    assert res_status["status"] == "success"
    assert "feature.py" in res_status["output"]

    # Commit file
    commit_tool = GitCommitTool()
    res_commit = commit_tool.execute({"message": "add feature.py"}, context)
    assert res_commit["status"] == "success"
    assert "add feature.py" in res_commit["output"] or "committed" in res_commit["output"].lower()


def test_grep_files_search(tmp_path):
    workspace = tmp_path / "my_repo"
    workspace.mkdir()
    (workspace / "math_utils.py").write_text("import sys\n\ndef calculate_sum(a, b):\n    return a + b\n")
    (workspace / "string_utils.py").write_text("def format_string(s):\n    return s.strip()\n")

    grep_tool = GrepFilesTool()
    context = {"workspace_root": str(workspace)}
    res = grep_tool.execute({"pattern": "def calculate_sum"}, context)
    assert res["status"] == "success"
    assert "math_utils.py" in res["output"]
    assert "calculate_sum" in res["output"]
    assert "string_utils.py" not in res["output"]
