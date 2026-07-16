"""
terminal_tools.py
-----------------
PulseCodeAI Sandboxed Tool System — Terminal Execution & Destructive Action Gate.
Provides subprocess management with hard timeouts and confirmation bridge gating.
"""
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional


class SecurityViolationError(Exception):
    """Raised when a command violates permission mode boundaries."""
    pass


@dataclass
class ConfirmationRequest:
    command: str
    reason: str
    is_destructive: bool = True


class ConfirmationBridge:
    """Inspects commands and enforces confirmation gates for mutating or destructive operations."""

    DESTRUCTIVE_PATTERNS = [
        r"\brm\s+-rf?\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\s+-fd?\b",
        r"\bgit\s+push\s+--force\b",
        r"\bdrop\s+table\b",
        r"\bdrop\s+database\b",
        r"\bkill\s+-9\b",
        r"\bmkfs\b",
        r"\beval\b"
    ]

    @classmethod
    def check_command(cls, command: str, mode: str = "normal") -> Optional[ConfirmationRequest]:
        """Check if command requires user confirmation or should be blocked by permission mode."""
        if mode == "plan":
            raise SecurityViolationError(f"Cannot run mutating/destructive command in plan mode: {command}")
        
        if mode in ("dont_ask", "bypass"):
            return None

        # Check against destructive patterns
        for pattern in cls.DESTRUCTIVE_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return ConfirmationRequest(
                    command=command,
                    reason=f"Destructive command detected matching pattern '{pattern}'",
                    is_destructive=True
                )

        # In normal mode, any shell command might mutate state, but we flag explicit destructive patterns high
        return None


class RunCommandTool:
    name = "run_command"
    description = "Execute a shell command inside the workspace with timeout protection."
    is_mutating = True

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        command = args.get("command", "")
        timeout = args.get("timeout", 60)
        if not command:
            return {"status": "error", "output": "Missing required parameter: 'command'"}

        workspace_root = context.get("workspace_root", ".")
        mode = context.get("permission_mode", "normal")

        try:
            # Check confirmation gate
            req = ConfirmationBridge.check_command(command, mode=mode)
            if req is not None and context.get("confirm_callback") is None:
                # If confirmation is needed and no callback is provided, return confirmation request status
                return {
                    "status": "requires_confirmation",
                    "command": command,
                    "reason": req.reason,
                    "is_destructive": req.is_destructive
                }

            # Execute subprocess with timeout
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            output = proc.stdout
            if proc.stderr:
                output += "\n[stderr]\n" + proc.stderr

            if proc.returncode != 0:
                return {"status": "error", "output": f"Command exited with code {proc.returncode}:\n{output}"}

            return {"status": "success", "output": output.strip()}

        except SecurityViolationError as exc:
            return {"status": "error", "output": f"SecurityViolationError: {exc}"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "output": f"Command timed out after {timeout} seconds: {command}"}
        except Exception as exc:
            return {"status": "error", "output": f"ExecutionError: {exc}"}
