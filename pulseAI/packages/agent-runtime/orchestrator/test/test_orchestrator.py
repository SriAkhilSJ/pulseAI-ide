"""
test_orchestrator.py
--------------------
TDD Unit Tests for PulseCodeAI Multi-Agent Orchestrator (`packages/agent-runtime/orchestrator`).
Verifies MissionManager checkpoint persistence, ReAct tool execution, and strict sub-agent allowlist enforcement.
"""
import pytest
from unittest.mock import MagicMock
from src.orchestrator import MissionManager, AgentOrchestrator, DispatchAgentTool


def test_mission_manager_save_and_load(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    
    mgr = MissionManager(workspace_root=str(workspace))
    messages = [
        {"role": "system", "content": "You are CoderAgent"},
        {"role": "user", "content": "Write auth.ts"}
    ]
    mgr.save_checkpoint("mission-001", turn_number=2, messages=messages, status="in_progress")
    
    loaded = mgr.load_checkpoint("mission-001")
    assert loaded is not None
    assert loaded["turn_number"] == 2
    assert loaded["messages"][1]["content"] == "Write auth.ts"
    assert loaded["status"] == "in_progress"


def test_orchestrator_tool_allowlist_isolation():
    # Setup mock ModelManager and UnifiedToolRegistry
    mock_model_mgr = MagicMock()
    mock_registry = MagicMock()
    
    # Simulate LLM returning a tool call to 'filesystem_write_file' during an Explore sub-agent run
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_123"
    mock_tool_call.function.name = "filesystem_write_file"
    mock_tool_call.function.arguments = '{"path": "test.txt", "content": "hacked"}'
    
    # First call returns the write tool call, second call returns final answer
    mock_choice_1 = MagicMock(message=MagicMock(content=None, tool_calls=[mock_tool_call]))
    mock_choice_2 = MagicMock(message=MagicMock(content="I cannot write files in Explore mode.", tool_calls=None))
    
    mock_model_mgr.complete.side_effect = [
        {"status": "success", "content": "", "raw_response": MagicMock(choices=[mock_choice_1])},
        {"status": "success", "content": "I cannot write files in Explore mode.", "raw_response": MagicMock(choices=[mock_choice_2])}
    ]
    
    orchestrator = AgentOrchestrator(model_manager=mock_model_mgr, tool_registry=mock_registry)
    
    # Run loop restricted only to read-only tools
    res = orchestrator.run_agent_loop(
        prompt="Explore the repo and write test.txt",
        model="groq/llama-3.3-70b-versatile",
        allowed_tools=["filesystem_read_file", "grep_files"]
    )
    
    assert res["status"] == "success"
    # Verify the tool registry was NEVER called for filesystem_write_file!
    mock_registry.execute.assert_not_called()
    # Verify the orchestrator handled the isolation blocked tool call cleanly
    assert "cannot write files" in res["final_answer"].lower()
