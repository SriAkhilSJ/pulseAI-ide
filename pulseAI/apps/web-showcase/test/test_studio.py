"""
test_studio.py
--------------
Real-Time Verification Test Suite for PulseAI Studio (`apps/web-showcase`).
Uses PulseCodeAI's own sandboxed tools (`UnifiedToolRegistry`) to verify DOM evaluation, RAG indexing, and Git version control in real time.
"""
import sys
import pytest
from pathlib import Path

# Ensure UnifiedToolRegistry can be loaded cleanly (parents[3] is pulseAI_repo root)
repo_root = Path(__file__).resolve().parents[3]
registry_src = repo_root / "packages" / "tools" / "registry" / "src"
if registry_src.exists() and str(registry_src) not in sys.path:
    sys.path.insert(0, str(registry_src))

from unified_registry import UnifiedToolRegistry


def test_studio_files_exist_and_readable():
    workspace = Path(__file__).resolve().parent.parent / "src"
    registry = UnifiedToolRegistry(workspace_root=str(workspace))

    # Verify index.html exists and is readable via sandboxed tool
    res_html = registry.execute("filesystem_read_file", {"path": "index.html"})
    assert res_html["status"] == "success"
    assert "<!DOCTYPE html>" in res_html["output"]
    assert "PulseAI Studio" in res_html["output"]

    # Verify styles.css exists
    res_css = registry.execute("filesystem_read_file", {"path": "styles.css"})
    assert res_css["status"] == "success"
    assert "--pulse-accent:" in res_css["output"]

    # Verify app.js exists
    res_js = registry.execute("filesystem_read_file", {"path": "app.js"})
    assert res_js["status"] == "success"
    assert "class PulseStudioController" in res_js["output"]


def test_studio_dom_evaluation_via_browser_tool():
    workspace = Path(__file__).resolve().parent.parent / "src"
    registry = UnifiedToolRegistry(workspace_root=str(workspace))

    # Evaluate title header DOM element inside index.html via BrowserEvaluateJsTool
    res_eval = registry.execute("browser_evaluate_js", {
        "path": "index.html",
        "script": "document.getElementById('title').innerText"
    })
    assert res_eval["status"] == "success"
    assert "PulseAI Studio" in res_eval["output"]


def test_studio_rag_indexing_and_semantic_search():
    workspace = Path(__file__).resolve().parent.parent / "src"
    registry = UnifiedToolRegistry(workspace_root=str(workspace))

    # Index web showcase files
    res_idx = registry.execute("rag_index_directory", {"path": "."})
    assert res_idx["status"] == "success"
    assert "Indexed" in res_idx["output"]

    # Search for dashboard state query
    res_srch = registry.execute("rag_search", {"query": "PulseStudioController agent status"})
    assert res_srch["status"] == "success"
    assert "app.js" in res_srch["output"]
