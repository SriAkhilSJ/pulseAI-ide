"""
Direct tests for permissions.py's mode logic, WITHOUT calling any real LLM
(isolates the enforcement/wiring bug class from LLM non-determinism --
same philosophy as test/subagents_test.py). A separate live test
(test/permissions_live_test.py) exercises this through real run_agent()
calls with a real LLM.

Run with: PYTHONPATH=/home/user/my-agent python3 test/permissions_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402 -- import first, same reason as subagents_test.py
import agent  # noqa: E402
import permissions  # noqa: E402
from permissions import PermissionEngine, PermissionMode, ALLOWED_IN_DONT_ASK  # noqa: E402


# ---------------------------------------------------------------------------
# default mode: byte-for-byte identical to today's existing behavior
# ---------------------------------------------------------------------------

def test_default_mode_is_unchanged_behavior():
    engine = PermissionEngine(PermissionMode.DEFAULT, base_confirm=lambda *a: True)
    # A destructive run_command call must still go through base_confirm
    # (here: auto-True), exactly like today.
    result = engine.confirm_fn("run_command", {"cmd": "rm -rf /tmp/x"}, "destructive", None)
    assert result is True

    engine_deny = PermissionEngine(PermissionMode.DEFAULT, base_confirm=lambda *a: False)
    result2 = engine_deny.confirm_fn("run_command", {"cmd": "rm -rf /tmp/x"}, "destructive", None)
    assert result2 is False

    tf, ts = engine.restricted_registry()
    assert tf is None and ts is None, "default mode must not restrict the registry at all"
    print("PASS: default mode defers entirely to base_confirm and never restricts the registry")


# ---------------------------------------------------------------------------
# plan mode: structural read-only enforcement
# ---------------------------------------------------------------------------

def test_plan_mode_registry_excludes_every_write_tool():
    engine = PermissionEngine(PermissionMode.PLAN)
    tool_functions, tool_specs = engine.restricted_registry()

    forbidden = {"write_file", "apply_edit", "run_command", "start_background_process",
                 "stop_background_process", "undo_last_edit", "git_commit", "git_create_branch",
                 "git_init", "generate_image", "filesystem_write_file", "dispatch_agent"}
    present = forbidden & set(tool_functions.keys())
    assert not present, f"plan mode registry must exclude all write/destructive tools, found: {present}"

    spec_names = {s["function"]["name"] for s in tool_specs}
    present_specs = forbidden & spec_names
    assert not present_specs, f"plan mode specs must exclude all write/destructive tools, found: {present_specs}"

    assert "read_file" in tool_functions
    assert "list_files" in tool_functions
    print("PASS: plan mode's registry structurally excludes every write/destructive tool")


def test_plan_mode_dispatch_tool_call_cannot_reach_write_file():
    """One level up: prove agent._dispatch_tool_call actually enforces
    this when fed plan mode's real restricted registry (not a synthetic
    fake one) -- the same wiring test pattern used for sub-agents."""
    engine = PermissionEngine(PermissionMode.PLAN)
    tool_functions, _ = engine.restricted_registry()

    result = agent._dispatch_tool_call(
        "write_file", '{"path": "test/scratch/plan_mode_escape.txt", "content": "should never land"}',
        confirm=lambda *a: True, tool_functions=tool_functions,
    )
    assert result == "ERROR: unknown tool 'write_file'", f"expected write_file unreachable, got: {result}"
    assert not os.path.exists("test/scratch/plan_mode_escape.txt"), "plan mode must never actually create this file"
    print("PASS: plan mode's registry makes write_file genuinely unreachable via the real dispatch chain")


# ---------------------------------------------------------------------------
# accept_edits mode: writes/edits auto-approved, destructive commands still gated
# ---------------------------------------------------------------------------

def test_accept_edits_auto_approves_ordinary_write_with_diff():
    engine = PermissionEngine(PermissionMode.ACCEPT_EDITS, base_confirm=lambda *a: False)
    # diff is not None -> ordinary content change -> auto-approved even
    # though base_confirm (what a human would be asked) would say False.
    result = engine.confirm_fn("write_file", {"path": "x.txt", "content": "new"}, "overwrite", "--- diff ---")
    assert result is True, "accept_edits must auto-approve an ordinary write with a real diff"
    print("PASS: accept_edits auto-approves an ordinary write/edit (diff present) without asking")


def test_accept_edits_still_gates_destructive_command():
    engine = PermissionEngine(PermissionMode.ACCEPT_EDITS, base_confirm=lambda *a: False)
    # run_command destructive calls always have diff=None -- must NOT be
    # auto-approved by accept_edits, must fall through to base_confirm.
    result = engine.confirm_fn("run_command", {"cmd": "rm -rf /"}, "destructive", None)
    assert result is False, "accept_edits must still gate destructive commands via base_confirm"
    print("PASS: accept_edits does not relax destructive run_command confirmation")


def test_accept_edits_still_gates_sensitive_path_write():
    """A write to a sensitive path always has diff=None (see
    _needs_confirmation's own docstring) -- accept_edits must not
    auto-approve this just because the tool name is write_file."""
    engine = PermissionEngine(PermissionMode.ACCEPT_EDITS, base_confirm=lambda *a: False)
    result = engine.confirm_fn("write_file", {"path": ".env", "content": "SECRET=1"}, "sensitive path", None)
    assert result is False, "accept_edits must not auto-approve a sensitive-path write (diff=None)"
    print("PASS: accept_edits does not auto-approve a sensitive-path write (correctly requires diff is not None)")


# ---------------------------------------------------------------------------
# auto mode: same write/edit relaxation, run_command never auto-relaxed further
# ---------------------------------------------------------------------------

def test_auto_mode_approves_ordinary_write_same_as_accept_edits():
    engine = PermissionEngine(PermissionMode.AUTO, base_confirm=lambda *a: False)
    result = engine.confirm_fn("apply_edit", {"path": "x.py"}, "edit", "--- diff ---")
    assert result is True
    print("PASS: auto mode auto-approves an ordinary edit (diff present) without asking")


def test_auto_mode_never_relaxes_destructive_command_further():
    engine = PermissionEngine(PermissionMode.AUTO, base_confirm=lambda *a: False)
    result = engine.confirm_fn("run_command", {"cmd": "sudo rm -rf /"}, "destructive", None)
    assert result is False, "auto mode must never auto-allow a destructive command -- no LLM classifier, no override"
    print("PASS: auto mode does not auto-allow destructive commands (deliberate deviation from a background-LLM-classifier design)")


# ---------------------------------------------------------------------------
# dont_ask mode: structural allow-list, deny outright, no prompt fallthrough
# ---------------------------------------------------------------------------

def test_dont_ask_registry_only_contains_allowed_tools():
    engine = PermissionEngine(PermissionMode.DONT_ASK)
    tool_functions, tool_specs = engine.restricted_registry()
    assert set(tool_functions.keys()) <= set(ALLOWED_IN_DONT_ASK)
    assert "write_file" not in tool_functions
    assert "run_command" not in tool_functions
    print(f"PASS: dont_ask mode's registry contains only allow-listed tools ({len(tool_functions)} tools)")


def test_dont_ask_dispatch_cannot_reach_disallowed_tool_no_prompt_ever_shown():
    """The critical property: a disallowed tool must be denied WITHOUT
    ever invoking confirm() at all -- proven by passing a confirm callable
    that raises if called, and confirming write_file still fails cleanly
    via the registry restriction, not via that confirm callable."""
    engine = PermissionEngine(PermissionMode.DONT_ASK)
    tool_functions, _ = engine.restricted_registry()

    def confirm_must_never_be_called(*a):
        raise AssertionError("confirm() must never be invoked in dont_ask mode for a disallowed tool")

    result = agent._dispatch_tool_call(
        "run_command", '{"cmd": "echo hi"}',
        confirm=confirm_must_never_be_called, tool_functions=tool_functions,
    )
    assert result == "ERROR: unknown tool 'run_command'", f"expected run_command unreachable, got: {result}"
    print("PASS: dont_ask mode denies a disallowed tool via the registry alone -- confirm() is never invoked, no prompt fallthrough")


def test_dont_ask_allows_a_real_read_only_call_to_actually_run():
    engine = PermissionEngine(PermissionMode.DONT_ASK)
    tool_functions, _ = engine.restricted_registry()
    result = agent._dispatch_tool_call(
        "list_files", '{"directory": "."}',
        confirm=lambda *a: True, tool_functions=tool_functions,
    )
    assert not result.startswith("ERROR"), f"an allow-listed tool must actually run in dont_ask mode: {result}"
    print("PASS: dont_ask mode still allows real, allow-listed read-only tools to actually execute")


# ---------------------------------------------------------------------------
# bypass mode: skips the PROMPT only, never the unconditional secret block
# ---------------------------------------------------------------------------

def test_bypass_mode_skips_the_prompt():
    engine = PermissionEngine(PermissionMode.BYPASS, base_confirm=lambda *a: False)
    # base_confirm would say False (deny) -- bypass must override that
    # WITHOUT even consulting base_confirm.
    result = engine.confirm_fn("run_command", {"cmd": "rm -rf /tmp/x"}, "destructive", None)
    assert result is True
    print("PASS: bypass mode approves without ever consulting base_confirm")


def test_bypass_never_touches_secret_paths_real_write_file_call():
    """THE critical safety property for this mode, tested against the
    REAL write_file() function (not a mock) -- bypass must never let a
    write to a sensitive path actually land on disk, because that
    protection lives inside tools.write_file itself (is_sensitive_path),
    completely independent of any confirm()/mode logic."""
    engine = PermissionEngine(PermissionMode.BYPASS)
    # bypass does not restrict the registry -- write_file is present.
    tool_functions, _ = engine.restricted_registry()
    assert tool_functions is None, "bypass must use the full, unrestricted registry"

    result = agent._dispatch_tool_call(
        "write_file", '{"path": ".env", "content": "PWNED=1"}',
        confirm=engine.confirm_fn,  # even with confirm always approving...
    )
    # ...the ACTUAL tool function write_file() still hard-refuses this,
    # unconditionally, before it ever touches disk.
    assert result.startswith("ERROR: refusing to write to sensitive path"), (
        f"CRITICAL: bypass mode must never let a write to .env actually succeed, got: {result}"
    )
    with open(".env", "r", encoding="utf-8") as f:
        content = f.read()
    assert "PWNED" not in content, "CRITICAL: .env was actually modified under bypass mode!"
    print("PASS: bypass mode never touches the unconditional secret-path block inside the real write_file() -- verified against the real .env file's real content")


def test_bypass_never_touches_secret_paths_real_read_file_call():
    engine = PermissionEngine(PermissionMode.BYPASS)
    result = agent._dispatch_tool_call(
        "read_file", '{"path": ".env"}',
        confirm=engine.confirm_fn,
    )
    assert result.startswith("ERROR: refusing to read sensitive path"), f"got: {result}"
    print("PASS: bypass mode never bypasses the unconditional secret-path block inside the real read_file() either")


# ---------------------------------------------------------------------------
# system_prompt_suffix: sanity (every mode must have one, no crashes)
# ---------------------------------------------------------------------------

def test_every_mode_has_a_system_prompt_suffix():
    for mode in PermissionMode:
        engine = PermissionEngine(mode)
        suffix = engine.system_prompt_suffix()
        assert isinstance(suffix, str) and len(suffix) > 10
    print("PASS: every mode produces a real, non-empty system prompt suffix")


# ---------------------------------------------------------------------------
# run_agent_with_mode: wiring sanity (no LLM -- checks it builds a valid
# call without crashing by monkeypatching agent.run_agent)
# ---------------------------------------------------------------------------

def test_run_agent_with_mode_wires_plan_mode_restricted_registry_through():
    captured = {}

    def fake_run_agent(user_input, **kwargs):
        captured.update(kwargs)
        return "fake reply"

    real_run_agent = agent.run_agent
    agent.run_agent = fake_run_agent
    try:
        reply = permissions.run_agent_with_mode("do something", mode=PermissionMode.PLAN)
    finally:
        agent.run_agent = real_run_agent

    assert reply == "fake reply"
    assert captured["tool_functions"] is not None
    assert "write_file" not in captured["tool_functions"]
    assert "PLAN MODE" in captured["system_prompt"]
    print("PASS: run_agent_with_mode correctly threads plan mode's restricted registry and system prompt into run_agent")


if __name__ == "__main__":
    test_default_mode_is_unchanged_behavior()
    test_plan_mode_registry_excludes_every_write_tool()
    test_plan_mode_dispatch_tool_call_cannot_reach_write_file()
    test_accept_edits_auto_approves_ordinary_write_with_diff()
    test_accept_edits_still_gates_destructive_command()
    test_accept_edits_still_gates_sensitive_path_write()
    test_auto_mode_approves_ordinary_write_same_as_accept_edits()
    test_auto_mode_never_relaxes_destructive_command_further()
    test_dont_ask_registry_only_contains_allowed_tools()
    test_dont_ask_dispatch_cannot_reach_disallowed_tool_no_prompt_ever_shown()
    test_dont_ask_allows_a_real_read_only_call_to_actually_run()
    test_bypass_mode_skips_the_prompt()
    test_bypass_never_touches_secret_paths_real_write_file_call()
    test_bypass_never_touches_secret_paths_real_read_file_call()
    test_every_mode_has_a_system_prompt_suffix()
    test_run_agent_with_mode_wires_plan_mode_restricted_registry_through()
    print("\nALL TESTS PASSED")
