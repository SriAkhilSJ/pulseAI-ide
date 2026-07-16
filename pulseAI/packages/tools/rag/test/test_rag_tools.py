"""
test_rag_tools.py
-----------------
TDD Unit Tests for PulseCodeAI RAG Indexing & Search (`packages/tools/rag`).
Verifies local embedding indexing and semantic retrieval.
"""
import pytest
from pathlib import Path
from src.rag_tools import RagIndexDirectoryTool, RagSearchTool


def test_rag_index_and_search(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    (workspace / "crypto.py").write_text("def get_bitcoin_price():\n    return 65000.0\n")
    (workspace / "weather.py").write_text("def get_temp():\n    return 72.5\n")

    index_tool = RagIndexDirectoryTool()
    context = {"workspace_root": str(workspace)}
    res_idx = index_tool.execute({"path": "."}, context)
    assert res_idx["status"] == "success"
    assert "indexed" in res_idx["output"].lower() or "files" in res_idx["output"].lower()

    search_tool = RagSearchTool()
    res_srch = search_tool.execute({"query": "external cryptocurrency price lookup"}, context)
    assert res_srch["status"] == "success"
    assert "crypto.py" in res_srch["output"]
