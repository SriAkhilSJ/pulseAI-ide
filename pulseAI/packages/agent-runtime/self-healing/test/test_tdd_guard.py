"""
test_tdd_guard.py
-----------------
TDD Unit Tests for PulseCodeAI TddGuard Automated Self-Healing Firewall (`packages/agent-runtime/self-healing`).
Verifies immediate linter/compiler interception on file modifications before user presentation.
"""
from unittest.mock import MagicMock
import pytest
from pathlib import Path
from src.tdd_guard import TddGuard


def test_tdd_guard_catches_syntax_error_and_prompts_healing(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    bad_py = workspace / "calc.py"

    # Mock registry where execute("filesystem_write_file") writes file and "lsp_get_diagnostics" returns SyntaxError
    mock_registry = MagicMock()
    def mock_exec(tool_name, args, context=None):
        if tool_name == "filesystem_write_file":
            bad_py.write_text(args.get("content", ""))
            return {"status": "success", "output": f"Wrote {args['path']}"}
        elif tool_name == "lsp_get_diagnostics":
            return {"status": "success", "output": "SyntaxError in calc.py line 2: invalid syntax"}
        return {"status": "success", "output": "OK"}
    
    mock_registry.execute.side_effect = mock_exec
    mock_registry.tools = {"filesystem_write_file": MagicMock(is_mutating=True), "lsp_get_diagnostics": MagicMock(is_mutating=False)}

    guard = TddGuard(tool_registry=mock_registry, max_healing_retries=2)
    
    res = guard.execute_guarded_tool("filesystem_write_file", {"path": "calc.py", "content": "def add(a):\n    return + +"})
    assert res["status"] == "healing_required"
    assert "SyntaxError" in res["diagnostic_output"]
    assert res["target_path"] == "calc.py"
