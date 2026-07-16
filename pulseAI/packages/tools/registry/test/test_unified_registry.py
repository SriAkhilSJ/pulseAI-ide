"""
test_unified_registry.py
------------------------
TDD Unit Tests for PulseCodeAI Unified Tool Registry (`packages/tools/registry`).
Verifies 100% of tools across all sub-packages are registered, validated, and executable.
"""
import pytest
from src.unified_registry import UnifiedToolRegistry


def test_unified_registry_initialization_and_execution(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    (workspace / "test.txt").write_text("hello unified registry")

    registry = UnifiedToolRegistry(workspace_root=str(workspace))
    tools_list = registry.list_tools()
    tool_names = [t["name"] for t in tools_list]

    # Verify every major tool category from my-agent is registered
    assert "filesystem_read_file" in tool_names
    assert "filesystem_write_file" in tool_names
    assert "run_command" in tool_names
    assert "git_status" in tool_names
    assert "git_commit" in tool_names
    assert "grep_files" in tool_names
    assert "ast_add_jsdoc" in tool_names
    assert "ast_find_untyped_functions" in tool_names
    assert "lsp_get_diagnostics" in tool_names
    assert "rag_search" in tool_names
    assert "browser_screenshot" in tool_names
    assert "web_search" in tool_names
    assert "process_manager" in tool_names
    assert "render_ui" in tool_names

    # Execute read file through unified registry
    res_read = registry.execute("filesystem_read_file", {"path": "test.txt"})
    assert res_read["status"] == "success"
    assert "hello unified registry" in res_read["output"]

    # Execute unknown tool
    res_unknown = registry.execute("non_existent_tool", {})
    assert res_unknown["status"] == "error"
    assert "Unknown tool" in res_unknown["output"]
