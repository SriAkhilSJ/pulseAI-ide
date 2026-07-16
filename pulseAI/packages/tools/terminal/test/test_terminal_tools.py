"""
test_terminal_tools.py
----------------------
TDD Unit Tests for PulseCodeAI Terminal Execution & Confirmation Bridge Gate.
Verifies command timeouts and interception of destructive/mutating commands.
"""
import pytest
from src.terminal_tools import ConfirmationBridge, ConfirmationRequest, RunCommandTool, SecurityViolationError


def test_confirmation_bridge_plan_mode_blocks():
    bridge = ConfirmationBridge()
    with pytest.raises(SecurityViolationError, match="Cannot run mutating/destructive command in plan mode"):
        bridge.check_command("rm -rf node_modules", mode="plan")


def test_confirmation_bridge_detects_destructive():
    bridge = ConfirmationBridge()
    req = bridge.check_command("rm -rf /tmp/test", mode="normal")
    assert isinstance(req, ConfirmationRequest)
    assert req.is_destructive is True
    assert "rm -rf" in req.reason or "Destructive" in req.reason


def test_confirmation_bridge_dont_ask_mode():
    bridge = ConfirmationBridge()
    req = bridge.check_command("rm -rf /tmp/test", mode="dont_ask")
    assert req is None  # Allowed cleanly without prompting


def test_run_command_timeout():
    tool = RunCommandTool()
    context = {"workspace_root": ".", "permission_mode": "dont_ask"}
    res = tool.execute({"command": "python3 -c 'import time; time.sleep(5)'", "timeout": 1}, context)
    assert res["status"] == "error"
    assert "Command timed out" in res["output"]


def test_run_command_success():
    tool = RunCommandTool()
    context = {"workspace_root": ".", "permission_mode": "dont_ask"}
    res = tool.execute({"command": "echo 'Hello PulseCode'"}, context)
    assert res["status"] == "success"
    assert "Hello PulseCode" in res["output"]
