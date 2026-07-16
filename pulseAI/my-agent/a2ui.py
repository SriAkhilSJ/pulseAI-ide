"""
a2ui.py
-------
Agent-to-UI: advisory, informational UI hints written to disk for a future
webview/extension host to read and render (progress indicators, diff
previews, chat-style tool summaries).

IMPORTANT SCOPE LIMIT, read before using this for anything security-
relevant: this module is ADVISORY ONLY. It has no connection whatsoever to
agent.py's real confirmation gate (_needs_confirmation / the `confirm`
callback passed into run_agent/_dispatch_tool_call). A proposed earlier
design conflated the two -- it had the agent call a `render_ui(template=
"CONFIRM_DIALOG", ...)` TOOL to "show" a confirmation card, but that tool
is just another LLM-callable function with zero coupling to the actual
synchronous confirm() gate that blocks _dispatch_tool_call before running
a destructive command. Nothing stops a model from calling that render tool
for show and then separately calling run_command right after -- the
"confirmation" would be pure decoration with no power to actually block
anything. That is a REAL safety regression, not just an incomplete
feature, so it's deliberately NOT replicated here.

For an actual, blocking, authoritative confirmation gate wired into
agent.py's real `confirm` parameter, see confirm_bridge.py instead --
a completely separate, purpose-built mechanism.

Design notes on what IS built here (informational hints only):
- File-based, not a webview-side fetch()/poll loop. A VS Code webview
  cannot fetch() an arbitrary local path at all -- it runs in a sandboxed
  iframe with no filesystem access; only resources explicitly passed
  through webview.asWebviewUri() (and declared in localResourceRoots) are
  reachable, and there is no bridge from a bare relative path to that
  sandbox. The REAL integration point is the EXTENSION HOST (a normal
  Node.js process, not sandboxed) reading this file -- via fs.watch, not
  setInterval-based polling from inside the webview -- and pushing updates
  into the webview via webview.postMessage(). This module only writes the
  file; it deliberately does not prescribe or assume any particular
  webview-side consumption mechanism, since that lives in extension code
  this project hasn't built yet.
- JSONL history + a single "current" pointer file, mirroring the same
  pattern missions.py already uses for progress.json/progress.md (a
  human/tool-inspectable snapshot, plus an append-only log for replay).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

VALID_TEMPLATES = {"PROGRESS", "DIFF_PREVIEW", "TOOL_RESULT", "CHAT_MESSAGE"}
# Deliberately NOT including CONFIRM_DIALOG here -- see module docstring.
# Any attempt to render one is rejected with a clear pointer to
# confirm_bridge.py instead of silently accepting a decorative, non-gating
# "confirmation" card.


class A2UI:
    def __init__(self, output_dir: str = ".agent_ui"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.current_path = self.output_dir / "current_manifest.json"
        self.history_path = self.output_dir / "manifest_history.jsonl"

    def render(
        self,
        template: str,
        data: dict,
        mission_id: str = "default",
    ) -> str:
        if template == "CONFIRM_DIALOG":
            return (
                "ERROR: CONFIRM_DIALOG is not a valid a2ui template. Advisory UI hints "
                "(this module) are not connected to the real confirmation gate and must "
                "never be used to imply user approval was obtained. Destructive/sensitive "
                "actions are already gated automatically by agent.py's confirm() callback "
                "-- see confirm_bridge.py if you need a UI-driven approval flow."
            )
        if template not in VALID_TEMPLATES:
            return f"ERROR: unknown template {template!r}. Valid: {sorted(VALID_TEMPLATES)}"

        manifest = {
            "version": "1.0",
            "template": template,
            "data": data,
            "mission_id": mission_id,
            "timestamp": time.time(),
        }
        self.current_path.write_text(json.dumps(manifest, indent=2))
        with open(self.history_path, "a") as f:
            f.write(json.dumps(manifest) + "\n")
        return f"UI hint rendered: {template}"

    def clear(self) -> None:
        if self.current_path.exists():
            self.current_path.unlink()

    def get_current(self) -> Optional[dict]:
        if self.current_path.exists():
            return json.loads(self.current_path.read_text())
        return None


a2ui = A2UI()


def render_ui(template: str, data: str, mission_id: str = "") -> str:
    """Tool: write an ADVISORY (non-blocking, non-authoritative) UI hint
    for a future webview to display -- progress, a diff preview, a tool
    result summary, or a chat-style message. This does NOT gate or pause
    execution and must never be used in place of the real confirmation
    system (see confirm_bridge.py) for destructive/sensitive actions."""
    try:
        data_dict = json.loads(data) if isinstance(data, str) else data
    except json.JSONDecodeError as e:
        return f"ERROR: invalid JSON in data: {e}"
    if not isinstance(data_dict, dict):
        return "ERROR: data must be a JSON object."
    return a2ui.render(template, data_dict, mission_id or "default")


def clear_ui() -> str:
    """Tool: clear the current advisory UI hint."""
    a2ui.clear()
    return "UI hint cleared"


TOOL_FUNCTIONS = {
    "render_ui": render_ui,
    "clear_ui": clear_ui,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "render_ui",
            "description": (
                "Show an ADVISORY, non-blocking UI hint in the webview (progress, a diff "
                "preview, a tool result summary, or a chat-style message). This is purely "
                "informational display -- it does NOT pause execution or request user "
                "approval. Destructive/sensitive actions are already gated automatically "
                "by the agent's own confirmation system; do not use this tool to represent "
                "or imply that a user has approved anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "enum": sorted(VALID_TEMPLATES),
                        "description": "Which advisory UI hint to show.",
                    },
                    "data": {
                        "type": "string",
                        "description": "JSON object string of template data.",
                    },
                    "mission_id": {"type": "string", "description": "Optional mission id for context."},
                },
                "required": ["template", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_ui",
            "description": "Clear the current advisory UI hint.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

A2UI_AVAILABLE = True
