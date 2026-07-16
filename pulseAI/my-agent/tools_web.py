"""
tools_web.py
------------
Web search tool: DuckDuckGo first (free, no key), Tavily as a fallback
(needs TAVILY_API_KEY, free tier ~1k searches/month, generally higher
quality/more current results).

Package note: the originally-proposed `duckduckgo_search` package is
DEPRECATED -- confirmed directly (RuntimeWarning + zero results returned
even though the call "succeeded"). The maintained replacement is `ddgs`
(same API surface, drop-in). requirements.txt uses `ddgs`, not
`duckduckgo_search`.
"""

from __future__ import annotations

import os
from typing import Optional

# Load .env independently of import order -- found by direct testing that
# calling web_search() (e.g. from a standalone script, or before
# llm_client.py happens to have been imported) left TAVILY_API_KEY unset
# even when it was genuinely present in .env, because dotenv-loading was
# previously only triggered as a side effect of importing llm_client.py.
# A tool module should not depend on some OTHER module having run first.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass  # python-dotenv not installed -- os.environ (real shell exports) still works

MAX_SNIPPET_CHARS = 300


def _format_results(results: list[dict], query: str) -> str:
    if not results:
        return f"No web results found for '{query}'."
    lines = [f"Web search results for '{query}':"]
    for r in results:
        title = r.get("title", "(no title)")
        snippet = (r.get("body") or r.get("content") or "").strip()
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS] + "..."
        url = r.get("href") or r.get("url") or "(no url)"
        lines.append(f"- **{title}**: {snippet}\n  URL: {url}")
    return "\n".join(lines)


def _search_duckduckgo(query: str, max_results: int) -> Optional[list[dict]]:
    """Try DuckDuckGo via the `ddgs` package. Returns None (not []) on any
    failure so the caller knows to try the Tavily fallback -- an empty list
    from DDG (genuinely no results) is a valid, different outcome from
    "DDG itself failed" and shouldn't trigger a fallback."""
    try:
        from ddgs import DDGS
    except ImportError:
        return None
    try:
        results = list(DDGS().text(query, max_results=max_results))
        return results
    except Exception:
        return None


def _search_tavily(query: str, max_results: int) -> str:
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        return (
            "ERROR: Web search failed. DuckDuckGo returned no usable results "
            "and TAVILY_API_KEY is not set (no fallback available)."
        )
    try:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": tavily_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return f"ERROR: Tavily search failed (HTTP {resp.status_code}): {resp.text[:300]}"
        data = resp.json()
        return _format_results(data.get("results", []), query)
    except Exception as e:
        return f"ERROR: Tavily search failed: {type(e).__name__}: {e}"


def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web for current information (docs, API references,
    up-to-date facts). Tries DuckDuckGo first (free, no API key needed);
    falls back to Tavily (needs TAVILY_API_KEY) if DuckDuckGo is
    unavailable/blocked/rate-limited, or returns nothing usable.
    """
    max_results = max(1, min(int(max_results), 10))  # sane bounds

    ddg_results = _search_duckduckgo(query, max_results)
    if ddg_results is not None and len(ddg_results) > 0:
        return _format_results(ddg_results, query)

    # DDG failed outright, OR returned zero results -- either way, give
    # Tavily a shot before giving up (a real, observed case: DDG can
    # silently return 0 results for a query that Tavily answers fine).
    return _search_tavily(query, max_results)


TOOL_FUNCTIONS = {"web_search": web_search}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information, API docs, or examples. "
                "Use when you need up-to-date facts, current syntax/API references, "
                "or anything you're not confident about from training data alone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-10). Defaults to 5.",
                    },
                },
                "required": ["query"],
            },
        },
    }
]
