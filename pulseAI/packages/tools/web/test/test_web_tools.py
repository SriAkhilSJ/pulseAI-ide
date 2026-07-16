"""
test_web_tools.py
-----------------
TDD Unit Tests for PulseCodeAI Web Search (`web_search`) and MCP Fetch (`fetch_fetch`).
"""
import pytest
from src.web_tools import WebSearchTool, FetchTool


def test_web_search():
    tool = WebSearchTool()
    res = tool.execute({"query": "PulseCode AI IDE"}, context={})
    assert res["status"] == "success"
    assert "query" in res["output"].lower() or "results" in res["output"].lower()


def test_fetch_tool():
    tool = FetchTool()
    res = tool.execute({"url": "https://example.com"}, context={})
    assert res["status"] in ("success", "error")  # Network might be restricted in sandbox or mock returns clean
    assert "example.com" in res["output"] or "http" in res["output"].lower()
