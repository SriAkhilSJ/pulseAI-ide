"""
test_filesystem_tools.py
------------------------
TDD Unit Tests for PulseCodeAI PathGuard Sandbox Security Engine & Filesystem Tools.
Verifies that path traversal attacks and sensitive credential access are hard-blocked.
"""
import os
import pytest
from pathlib import Path
from src.filesystem_tools import PathGuard, SecurityViolationError, ToolRegistry, ReadFileTool, WriteFileTool


def test_path_guard_blocks_traversal(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    
    # Attempt absolute path outside workspace
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("root password")
    
    with pytest.raises(SecurityViolationError, match="Path traversal outside workspace root"):
        PathGuard.assert_safe_path(str(outside_file), str(workspace))
        
    # Attempt relative traversal
    with pytest.raises(SecurityViolationError, match="Path traversal outside workspace root"):
        PathGuard.assert_safe_path("../../secret.txt", str(workspace))


def test_path_guard_blocks_sensitive_credentials(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    
    env_file = workspace / ".env"
    env_file.write_text("API_KEY=12345")
    
    with pytest.raises(SecurityViolationError, match="Access to sensitive credential path is hard-blocked"):
        PathGuard.assert_safe_path(".env", str(workspace))
        
    git_cred = workspace / ".git/credentials"
    with pytest.raises(SecurityViolationError, match="Access to sensitive credential path is hard-blocked"):
        PathGuard.assert_safe_path(".git/credentials", str(workspace))


def test_path_guard_allows_safe_file(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    safe_file = workspace / "src" / "app.py"
    
    resolved = PathGuard.assert_safe_path("src/app.py", str(workspace))
    assert resolved == safe_file.resolve()


def test_tool_registry_and_read_write(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    
    registry = ToolRegistry(workspace_root=str(workspace))
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    
    # Write safe file via tool
    write_res = registry.execute("filesystem_write_file", {"path": "test.txt", "content": "Hello World"})
    assert write_res["status"] == "success"
    assert (workspace / "test.txt").read_text() == "Hello World"
    
    # Read file via tool
    read_res = registry.execute("filesystem_read_file", {"path": "test.txt"})
    assert read_res["status"] == "success"
    assert "Hello World" in read_res["output"]
    
    # Attempt malicious write via tool
    bad_res = registry.execute("filesystem_write_file", {"path": ".env", "content": "HACKED=1"})
    assert bad_res["status"] == "error"
    assert "SecurityViolationError" in bad_res["output"]
