"""
test_orchestrator_live.py
-------------------------
Live Verification Test (`<real test>`) for PulseCodeAI AgentOrchestrator.
Runs a real multi-turn ReAct loop connecting to Groq / OpenRouter over the wire, invoking real sandboxed tools.
"""
import os
import sys
import pytest
from pathlib import Path

# Ensure registry and ai-core paths can be resolved cleanly
curr_root = Path(__file__).resolve().parents[3]
for sub_path in (
    curr_root / "tools" / "registry" / "src",
    curr_root / "ai-core" / "models" / "src"
):
    if sub_path.exists() and str(sub_path) not in sys.path:
        sys.path.insert(0, str(sub_path))

from src.orchestrator import AgentOrchestrator
from model_manager import ModelManager
from unified_registry import UnifiedToolRegistry


def _load_env_keys():
    env_path = Path("/home/user/pulseAI_repo/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val


@pytest.mark.live
def test_live_orchestrator_multi_turn_tool_execution(tmp_path):
    _load_env_keys()
    assert any(k in os.environ for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY"))

    # Create dummy file to be read by the live agent loop
    workspace = tmp_path / "live_project"
    workspace.mkdir()
    secret_file = workspace / "agent_secret.txt"
    secret_file.write_text("PULSE_ORCHESTRATOR_SUCCESS_2026")

    model_mgr = ModelManager()
    registry = UnifiedToolRegistry(workspace_root=str(workspace))

    orchestrator = AgentOrchestrator(model_manager=model_mgr, tool_registry=registry)

    # Prompt requiring real tool execution
    prompt = (
        "You have access to the tool `filesystem_read_file`. "
        "Call `filesystem_read_file` with arguments `{\"path\": \"agent_secret.txt\"}`. "
        "After you get the output from the tool, reply with exactly what was inside the file."
    )

    result = orchestrator.run_agent_loop(
        prompt=prompt,
        model="groq/llama-3.3-70b-versatile",
        allowed_tools=["filesystem_read_file"],
        max_turns=5
    )

    assert result["status"] == "success"
    assert "PULSE_ORCHESTRATOR_SUCCESS_2026" in result["final_answer"]
    assert result["turns_executed"] >= 2  # Turn 1: tool call -> Turn 2: final answer
    print(f"\n[LIVE ORCHESTRATOR VERIFIED] Turns executed: {result['turns_executed']} | Final Answer: '{result['final_answer']}'")
