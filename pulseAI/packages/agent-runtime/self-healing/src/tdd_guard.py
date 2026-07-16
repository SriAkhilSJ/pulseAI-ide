"""
tdd_guard.py
------------
PulseCodeAI Automated TDD Self-Healing Firewall (`packages/agent-runtime/self-healing`).
Intercepts file modifications and triggers immediate local ReAct self-correction on linter errors.
"""
from typing import Any, Dict, List, Optional


class TddGuard:
    """Monitors mutating tool executions and intercepts diagnostic failures (`RED`) to force local self-healing (`GREEN`)."""

    def __init__(self, tool_registry: Any, max_healing_retries: int = 2):
        self.tool_registry = tool_registry
        self.max_healing_retries = max_healing_retries

    def execute_guarded_tool(self, tool_name: str, args: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a tool via UnifiedToolRegistry. If mutating, run diagnostic checks and flag if self-healing is required."""
        ctx = context or {}
        tool_obj = getattr(self.tool_registry, "tools", {}).get(tool_name)
        is_mutating = getattr(tool_obj, "is_mutating", False) if tool_obj else False

        # Execute primary tool
        res = self.tool_registry.execute(tool_name, args, ctx)
        if res.get("status") != "success" or not is_mutating:
            return res

        # If file write or edit occurred, run immediate lsp_get_diagnostics check
        target_path = args.get("path", "")
        if not target_path or not hasattr(self.tool_registry, "execute"):
            return res

        diag_res = self.tool_registry.execute("lsp_get_diagnostics", {"path": target_path}, ctx)
        diag_out = str(diag_res.get("output", ""))

        # Check for syntax/linter errors
        if "SyntaxError" in diag_out or "error" in diag_out.lower() and "0 syntax errors" not in diag_out.lower() and "clean" not in diag_out.lower():
            return {
                "status": "healing_required",
                "output": f"Guarded write succeeded on {target_path}, but introduced diagnostic error: {diag_out}",
                "target_path": target_path,
                "diagnostic_output": diag_out,
                "max_retries": self.max_healing_retries
            }

        return res
