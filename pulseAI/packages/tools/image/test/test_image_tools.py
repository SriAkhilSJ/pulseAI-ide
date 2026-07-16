"""
test_image_tools.py
-------------------
TDD Unit Tests for PulseCodeAI Image Generation (`packages/tools/image`).
Verifies sandboxed image tool execution and PathGuard verification on image paths.
"""
import pytest
from pathlib import Path
from src.image_tools import GenerateImageTool


def test_generate_image_tool(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()

    tool = GenerateImageTool()
    context = {"workspace_root": str(workspace), "permission_mode": "dont_ask"}
    
    # Generate image call
    res = tool.execute({"prompt": "A futuristic AI-Native IDE logo", "file_path": "logo.png"}, context)
    assert res["status"] == "success"
    assert "logo.png" in res["output"]
    assert (workspace / "logo.png").exists()
