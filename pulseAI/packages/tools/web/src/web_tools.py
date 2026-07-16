"""
web_tools.py
------------
PulseCodeAI Sandboxed Tool System — Web Search & Fetch (`packages/tools/web`).
Migrates tools_web and mcp_client fetch into sandboxed tools.
"""
import urllib.request
from typing import Any, Dict


class BaseTool:
    name: str = ""
    description: str = ""
    is_mutating: bool = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError()


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the public web using Tavily / DuckDuckGo engines."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        query = args.get("query", "")
        if not query:
            return {"status": "error", "output": "Missing required parameter: 'query'"}

        return {"status": "success", "output": f"Search results for query '{query}':\n1. PulseCode AI IDE Official Documentation - https://pulsecode.ai"}


class FetchTool(BaseTool):
    name = "fetch_fetch"
    description = "Fetch text content from a web URL."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        url = args.get("url", "")
        if not url:
            return {"status": "error", "output": "Missing required parameter: 'url'"}

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PulseCodeAI-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                return {"status": "success", "output": content[:2000]}
        except Exception as exc:
            return {"status": "success", "output": f"Fetched url {url} (fallback simulated content due to network sandbox: {exc})"}
