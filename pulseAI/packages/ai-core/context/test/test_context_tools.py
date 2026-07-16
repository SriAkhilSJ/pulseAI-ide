"""
test_context_tools.py
---------------------
TDD Unit Tests for PulseCodeAI ContextManager & ContextCompressor.
Verifies AST dependency map generation and automatic token limit compression.
"""
import pytest
from src.context_tools import ContextCompressor, ContextManager


def test_context_compressor_triggers():
    messages = [
        {"role": "system", "content": "System prompt"}
    ]
    # Add 20 long turns
    for i in range(20):
        messages.append({"role": "user", "content": f"Turn {i} user input " * 50})
        messages.append({"role": "assistant", "content": f"Turn {i} assistant reply " * 50})

    compressor = ContextCompressor(max_tokens=1000, threshold_ratio=0.5)
    assert compressor.should_compress(messages) is True

    compressed = compressor.compress(messages, keep_recent_turns=4)
    # Check system message is preserved
    assert compressed[0]["role"] == "system"
    assert compressed[0]["content"] == "System prompt"
    # Check summary message is injected at index 1
    assert compressed[1]["role"] == "system"
    assert "Compacted History Summary" in compressed[1]["content"]
    # Check only recent turns remain after summary
    assert len(compressed) == 1 + 1 + (4 * 2)  # system + summary + 4 turns (user+assistant)


def test_context_manager_workspace_map(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    (workspace / "main.py").write_text("import utils\ndef main():\n    pass\n")
    (workspace / "utils.py").write_text("def helper():\n    return True\n")

    mgr = ContextManager(workspace_root=str(workspace))
    repo_map = mgr.get_workspace_map(token_budget=500)
    assert "main.py" in repo_map
    assert "utils.py" in repo_map
