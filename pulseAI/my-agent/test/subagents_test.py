"""
Direct tests for subagents.py's structural restriction/depth/budget logic,
WITHOUT calling any real LLM (isolates the wiring/enforcement bug class
from LLM non-determinism -- same philosophy as
test/agent_streaming_wiring_test.py and test/null_args_dispatch_test.py).

A separate live end-to-end test (test/subagents_live_test.py) exercises
this through a real dispatch_agent() call with a real LLM, proving the
restriction holds even when a real model tries to call a disallowed tool.

Run with: PYTHONPATH=/home/user/my-agent python3 test/subagents_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402 -- import first so subagents.py's lazy `import tools` inside _restricted_registry succeeds
import subagents  # noqa: E402


def test_explore_registry_excludes_write_tools():
    """The core safety property: an 'explore' sub-agent's tool_functions
    dict must not even CONTAIN write_file/apply_edit/run_command as keys --
    not just be told not to use them."""
    tool_functions, tool_specs, system_prompt = subagents._restricted_registry("explore", subagent_depth=0)

    forbidden = {"write_file", "apply_edit", "run_command", "start_background_process",
                 "stop_background_process", "undo_last_edit", "git_commit", "git_create_branch",
                 "git_init", "generate_image", "filesystem_write_file"}
    present_forbidden = forbidden & set(tool_functions.keys())
    assert not present_forbidden, f"explore sub-agent registry must not contain write tools, found: {present_forbidden}"

    # Also check the SPECS (what the model is even told exists) exclude them.
    spec_names = {s["function"]["name"] for s in tool_specs}
    present_forbidden_specs = forbidden & spec_names
    assert not present_forbidden_specs, f"explore sub-agent specs must not list write tools, found: {present_forbidden_specs}"

    # But it SHOULD have real read-only tools (whichever are actually registered
    # in this environment -- read_file/list_files/grep_files are always native).
    assert "read_file" in tool_functions
    assert "list_files" in tool_functions
    assert "grep_files" in tool_functions
    assert "read_file" in spec_names

    print("PASS: 'explore' sub-agent registry structurally excludes every write/destructive tool")


def test_general_purpose_gets_everything_except_dispatch_agent_at_max_depth():
    """general-purpose gets the FULL parent tool set (minus dispatch_agent,
    since depth 0 + 1 == MAX_SUBAGENT_DEPTH by default -- can't nest further)."""
    tool_functions, tool_specs, _ = subagents._restricted_registry("general-purpose", subagent_depth=0)

    assert "write_file" in tool_functions, "general-purpose must retain write access"
    assert "run_command" in tool_functions, "general-purpose must retain run_command"

    # At the default MAX_SUBAGENT_DEPTH=1, a depth-0 sub-agent is ALREADY at
    # the ceiling for further nesting (0 + 1 is not < 1) -- dispatch_agent
    # must be absent from its own registry.
    assert "dispatch_agent" not in tool_functions, (
        "a sub-agent at the nesting ceiling must not be able to dispatch a further sub-agent"
    )
    print("PASS: 'general-purpose' sub-agent keeps full tool access but cannot nest further sub-agents at the depth ceiling")


def test_unknown_subagent_type_rejected_before_any_llm_call():
    """dispatch_agent() must reject an invalid subagent_type immediately,
    without spending an LLM call or touching the budget."""
    budget = subagents.SubagentBudget(max_subagents=4)
    result = subagents.dispatch_agent(
        "do something", subagent_type="nonexistent-type", _subagent_budget=budget,
    )
    assert result.startswith("ERROR"), f"expected an ERROR for unknown subagent_type, got: {result}"
    assert budget.remaining() == 4, "an invalid subagent_type must not consume the budget"
    print("PASS: unknown subagent_type is rejected before touching the sub-agent budget")


def test_depth_ceiling_rejects_before_budget_or_llm_call():
    """A sub-agent already at MAX_SUBAGENT_DEPTH must be refused outright --
    this is the real enforcement of 'sub-agents cannot spawn further
    sub-agents', tested directly against dispatch_agent(), not just the
    registry-building helper above."""
    budget = subagents.SubagentBudget(max_subagents=4)
    result = subagents.dispatch_agent(
        "do something", subagent_type="general-purpose",
        _subagent_depth=subagents.MAX_SUBAGENT_DEPTH,  # already at the ceiling
        _subagent_budget=budget,
    )
    assert result.startswith("ERROR"), f"expected a depth-ceiling ERROR, got: {result}"
    assert "depth" in result.lower()
    assert budget.remaining() == 4, "a depth-rejected call must not consume the budget"
    print("PASS: dispatch_agent refuses to run at/past MAX_SUBAGENT_DEPTH, without touching the budget")


def test_budget_exhaustion_real_object_not_mocked():
    """A REAL SubagentBudget object, shared across several dispatch_agent
    calls the way agent.py actually threads it through _dispatch_tool_call,
    must refuse the call once exhausted -- proven by acquiring it down to
    zero via its real try_acquire() method, not by asserting on a mock."""
    budget = subagents.SubagentBudget(max_subagents=2)
    assert budget.try_acquire() is True
    assert budget.try_acquire() is True
    assert budget.remaining() == 0

    # A third real dispatch_agent() call sharing this same exhausted budget
    # object must be refused BEFORE it would attempt any LLM call (we pass
    # subagent_type="explore" so if the budget check were skipped, this
    # would actually try to import agent.py and run a real ReAct loop --
    # the test would then hang/fail on missing LLM keys, which is itself
    # evidence the budget check didn't fire first).
    result = subagents.dispatch_agent(
        "do something", subagent_type="explore", _subagent_budget=budget,
    )
    assert result.startswith("ERROR"), f"expected a budget-exhausted ERROR, got: {result}"
    assert "budget" in result.lower()
    print("PASS: a real, shared SubagentBudget genuinely blocks a 3rd dispatch once exhausted")


def test_tool_spec_schema_has_no_internal_params():
    """The model-visible TOOL_SPECS entry for dispatch_agent must NOT
    expose _confirm/_subagent_depth/_subagent_budget -- these are
    Python-only injection parameters (same pattern as run_command's
    on_line), never something the LLM can set. Mirrors
    test_llm_tool_spec_cannot_set_on_line's philosophy from the streaming
    feature's own test suite."""
    spec = next(s for s in subagents.TOOL_SPECS if s["function"]["name"] == "dispatch_agent")
    props = spec["function"]["parameters"]["properties"]
    assert "_confirm" not in props
    assert "_subagent_depth" not in props
    assert "_subagent_budget" not in props
    # agent_name was added deliberately (custom_agents.py, .agent_agents/*.md)
    # as a genuine, MODEL-VISIBLE parameter -- not an internal injection
    # param like the three checked above -- so it's expected here.
    assert set(props.keys()) == {"prompt", "subagent_type", "agent_name", "description"}
    print("PASS: dispatch_agent's LLM-visible schema exposes only prompt/subagent_type/agent_name/description")


def test_agent_dispatch_tool_call_restricts_registry_end_to_end():
    """One level up: prove agent._dispatch_tool_call's new `tool_functions`
    parameter actually restricts what a call can reach, using a
    synthetic/fake registry (no LLM, no sub-agent involved) -- isolates
    the WIRING (agent.py correctly uses the passed-in registry instead of
    the global one) from subagents.py's own logic, tested separately above."""
    import agent

    fake_registry = {"read_file": lambda path: f"fake read of {path}"}
    # write_file exists in the REAL global registry but must be unreachable
    # when a restricted registry is passed in.
    result = agent._dispatch_tool_call(
        "write_file", '{"path": "x.txt", "content": "y"}',
        confirm=lambda *a: True, tool_functions=fake_registry,
    )
    assert result == "ERROR: unknown tool 'write_file'", f"expected write_file to be unreachable, got: {result}"

    result2 = agent._dispatch_tool_call(
        "read_file", '{"path": "whatever.txt"}',
        confirm=lambda *a: True, tool_functions=fake_registry,
    )
    assert result2 == "fake read of whatever.txt"
    print("PASS: agent._dispatch_tool_call's tool_functions parameter genuinely restricts what's callable")


if __name__ == "__main__":
    test_explore_registry_excludes_write_tools()
    test_general_purpose_gets_everything_except_dispatch_agent_at_max_depth()
    test_unknown_subagent_type_rejected_before_any_llm_call()
    test_depth_ceiling_rejects_before_budget_or_llm_call()
    test_budget_exhaustion_real_object_not_mocked()
    test_tool_spec_schema_has_no_internal_params()
    test_agent_dispatch_tool_call_restricts_registry_end_to_end()
    print("\nALL TESTS PASSED")
