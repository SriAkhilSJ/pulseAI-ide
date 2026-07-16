"""
ui_tools.py
-----------
PulseCodeAI Sandboxed Tool System — Agent-to-UI (`a2ui`) Streaming Markers & Render Tools (`packages/tools/ui`).
Migrates a2ui into sandboxed tools.
"""
import json
from typing import Any, Dict


class BaseTool:
    name: str = ""
    description: str = ""
    is_mutating: bool = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError()


class RenderUiTool(BaseTool):
    name = "render_ui"
    description = "Emit structured Agent-to-UI component rendering markers."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        component = args.get("component", "")
        props = args.get("props", {})
        if not component:
            return {"status": "error", "output": "Missing required parameter: 'component'"}

        marker = f"<a2ui:{component} props='{json.dumps(props)}' />"
        return {"status": "success", "output": marker}


class ClearUiTool(BaseTool):
    name = "clear_ui"
    description = "Clear all active Agent-to-UI markers from the webview."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "<a2ui:clear /> UI markers cleared."}
