"""
test_symbol_engine.py
---------------------
TDD Unit Tests for PulseCodeAI Symbol & Call-Graph Engine (`packages/ai-core/symbol-engine`).
Verifies real-time symbol extraction and caller impact warnings to guide free/smaller LLM models cleanly.
"""
import pytest
from pathlib import Path
from src.symbol_engine import SymbolEngine


def test_symbol_graph_and_impact_warnings(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    (workspace / "auth.py").write_text("def verify_jwt(token):\n    return True\n")
    (workspace / "main.py").write_text("from auth import verify_jwt\n\ndef run_app():\n    if verify_jwt('abc'):\n        pass\n")

    engine = SymbolEngine(workspace_root=str(workspace))
    graph = engine.build_graph()

    assert len(graph.nodes) >= 2
    assert "verify_jwt" in engine.symbols
    assert "run_app" in engine.symbols

    warnings = engine.get_impact_warnings(file_path="auth.py", symbol_name="verify_jwt")
    assert len(warnings) >= 1
    assert any("run_app" in w for w in warnings)
