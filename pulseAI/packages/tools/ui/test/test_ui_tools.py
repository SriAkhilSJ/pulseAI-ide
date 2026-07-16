"""
test_ui_tools.py
----------------
TDD Unit Tests for PulseCodeAI Agent-to-UI (`a2ui`) Streaming Markers and UI Render Tools (`packages/tools/ui`).
"""
import pytest
from src.ui_tools import RenderUiTool, ClearUiTool


def test_render_and_clear_ui():
    render = RenderUiTool()
    res_r = render.execute({"component": "PlanChecklist", "props": {"steps": ["Step 1", "Step 2"]}}, context={})
    assert res_r["status"] == "success"
    assert "<a2ui:" in res_r["output"] or "PlanChecklist" in res_r["output"]

    clear = ClearUiTool()
    res_c = clear.execute({}, context={})
    assert res_c["status"] == "success"
    assert "cleared" in res_c["output"].lower()
