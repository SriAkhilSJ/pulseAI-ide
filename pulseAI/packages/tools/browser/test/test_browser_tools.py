"""
test_browser_tools.py
---------------------
TDD Unit Tests for PulseCodeAI Playwright Headless Browser & Vision Tools (`packages/tools/browser`).
Verifies local HTML evaluation and screenshot generation.
"""
import pytest
from pathlib import Path
from src.browser_tools import BrowserEvaluateJsTool, BrowserScreenshotTool


def test_browser_evaluate_and_screenshot(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    html_file = workspace / "index.html"
    html_file.write_text("<html><body><h1 id='title'>PulseCodeAI</h1></body></html>")

    eval_tool = BrowserEvaluateJsTool()
    context = {"workspace_root": str(workspace)}
    res_eval = eval_tool.execute({"path": "index.html", "script": "document.getElementById('title').innerText"}, context)
    assert res_eval["status"] == "success"
    assert "PulseCodeAI" in res_eval["output"]

    screenshot_tool = BrowserScreenshotTool()
    res_shot = screenshot_tool.execute({"path": "index.html"}, context)
    assert res_shot["status"] == "success"
    assert "screenshot" in res_shot["output"].lower() or "base64" in res_shot["output"].lower()
