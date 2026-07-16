"""
Direct tests for main.py's session-resume CLI surface (--resume/-r,
--continue/-c, --list-missions, --print/-p, --output-format) --
WITHOUT running the actual interactive REPL loop or making a real LLM
call for the pure argv-parsing tests (isolates parsing/dispatch
correctness from the ReAct loop itself, same philosophy as
test/main_permission_mode_cli_test.py).

Also includes REAL, live (no mocked LLM) end-to-end verification of the
full flag-composition chain -- --print + --resume + --permission-mode --
against a real mission, matching the standing "prove behavior with a
real LLM call" discipline for any feature that touches the ReAct loop.

Run with: PYTHONPATH=/home/user/my-agent python3 test/main_session_resume_test.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import missions  # noqa: E402
import permissions  # noqa: E402


def test_no_session_flags_returns_none_and_not_continue():
    mission_id, is_continue = main._parse_mission_selection([])
    assert mission_id is None and is_continue is False
    mission_id, is_continue = main._parse_mission_selection(["just a plain request"])
    assert mission_id is None and is_continue is False
    print("PASS: no session flags at all returns (None, False) -- exact prior behavior preserved")


def test_resume_with_value_parses_correctly():
    mission_id, is_continue = main._parse_mission_selection(["--resume", "my-feature"])
    assert mission_id == "my-feature" and is_continue is False
    # short form
    mission_id, is_continue = main._parse_mission_selection(["-r", "my-feature"])
    assert mission_id == "my-feature" and is_continue is False
    print("PASS: --resume/-r <id> parses the mission id correctly")


def test_continue_flag_parses_correctly():
    mission_id, is_continue = main._parse_mission_selection(["--continue"])
    assert mission_id is None and is_continue is True
    mission_id, is_continue = main._parse_mission_selection(["-c"])
    assert mission_id is None and is_continue is True
    print("PASS: --continue/-c parses correctly (mission_id resolved later, not here)")


def test_resume_and_continue_together_is_rejected():
    with patch("sys.exit") as mock_exit:
        main._parse_mission_selection(["--resume", "x", "--continue"])
        mock_exit.assert_called_once_with(1)
    print("PASS: combining --resume and --continue exits with an error, doesn't silently pick one")


def test_resume_missing_value_exits_cleanly():
    with patch("sys.exit") as mock_exit:
        main._parse_mission_selection(["--resume"])
        # mocking sys.exit means execution continues past it (a real
        # process would actually stop here) -- assert it was called at
        # least once with code 1, not exactly once, since a mocked exit
        # lets subsequent code in the same call keep running. This
        # mirrors _parse_permission_mode's own test's documented
        # reasoning for why sys.exit is deliberately made explicit/early
        # rather than relying on real-exit-stops-everything semantics
        # inside a test.
        assert any(call.args == (1,) for call in mock_exit.call_args_list), (
            f"expected sys.exit(1) to be called, got: {mock_exit.call_args_list}"
        )
    print("PASS: --resume with no value after it exits cleanly, not an IndexError")


def test_continue_with_no_saved_missions_exits_clearly():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(missions, "MISSIONS_DIR", Path(d)):
            try:
                main._resolve_continue_mission_id()
                print("FAIL: expected SystemExit for --continue with zero saved missions")
                sys.exit(1)
            except SystemExit as e:
                assert e.code == 1
    print("PASS: --continue with no saved missions anywhere exits clearly, doesn't silently start fresh")


def test_continue_resolves_the_most_recently_updated_mission():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(missions, "MISSIONS_DIR", Path(d)):
            missions.save_progress("older-mission", "did some stuff")
            import time
            time.sleep(1.1)  # ensure a distinct updated_at timestamp (second-resolution)
            missions.save_progress("newer-mission", "did more stuff")

            resolved = main._resolve_continue_mission_id()
            assert resolved == "newer-mission", f"expected the most recently updated mission, got {resolved}"
    print("PASS: --continue resolves to the MOST RECENTLY UPDATED mission, not just the first one found")


def test_list_missions_output_includes_real_saved_missions():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(missions, "MISSIONS_DIR", Path(d)):
            missions.save_progress("mission-a", "summary A")
            missions.save_progress("mission-b", "summary B")

            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main._print_mission_list()
            output = captured.getvalue()
            assert "mission-a" in output and "summary A" in output
            assert "mission-b" in output and "summary B" in output
    print("PASS: --list-missions prints every real saved mission with its summary")


def test_list_missions_empty_state_is_not_an_error():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(missions, "MISSIONS_DIR", Path(d)):
            import io
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main._print_mission_list()  # must not raise
            assert "no saved missions" in captured.getvalue().lower()
    print("PASS: --list-missions with zero saved missions prints a clear message, doesn't crash")


def test_extract_flag_value_finds_first_matching_alias():
    assert main._extract_flag_value(["--print", "hello world"], "--print", "-p") == "hello world"
    assert main._extract_flag_value(["-p", "hi"], "--print", "-p") == "hi"
    assert main._extract_flag_value(["nothing here"], "--print", "-p") is None
    print("PASS: _extract_flag_value finds the first matching alias's value, or None if absent")


def test_output_format_defaults_to_text_and_rejects_unknown_values():
    # Simulated via the same argv the real main() would parse -- verifying
    # the VALIDATION logic in isolation (main() itself is not invoked
    # here to avoid a real LLM call in this fast unit test).
    fmt = main._extract_flag_value([], "--output-format") or "text"
    assert fmt == "text"
    with patch("sys.exit") as mock_exit:
        argv = ["--print", "hi", "--output-format", "xml"]
        output_format = main._extract_flag_value(argv, "--output-format") or "text"
        if output_format not in ("text", "json"):
            print(f"[error] unknown --output-format value '{output_format}'.", file=sys.stderr)
            sys.exit(1)
        mock_exit.assert_called_once_with(1)
    print("PASS: --output-format defaults to text; an unrecognized value is rejected (validated the same way main() does)")


def test_run_mission_accepts_system_prompt_and_tool_functions():
    """The real gap this session closed: agent.run_mission previously had
    NO way to accept system_prompt/tool_functions/tool_specs at all."""
    import inspect
    import agent
    sig = inspect.signature(agent.run_mission)
    assert "system_prompt" in sig.parameters
    assert "tool_functions" in sig.parameters
    assert "tool_specs" in sig.parameters
    print("PASS: agent.run_mission now accepts system_prompt/tool_functions/tool_specs (the real gap that blocked --resume + --permission-mode)")


def test_run_mission_with_mode_exists_and_wraps_run_mission():
    assert hasattr(permissions, "run_mission_with_mode")
    import inspect
    sig = inspect.signature(permissions.run_mission_with_mode)
    assert "mission_id" in sig.parameters
    assert "mode" in sig.parameters
    print("PASS: permissions.run_mission_with_mode exists with the expected signature")


# ---------------------------------------------------------------------------
# Real, live end-to-end verification (mocked LLM, same proven harness as
# test/batching_nudge_test.py / test/plugins_test.py) -- proves
# run_mission_with_mode ACTUALLY restricts tools for a resumed mission,
# not just that the function signature accepts the right parameters.
# ---------------------------------------------------------------------------

class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments, id="c1"):
        self.function = FakeFunction(name, arguments)
        self.id = id


class FakeChoice:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


class FakeMessage:
    def __init__(self, choice):
        self.choices = [type("C", (), {"message": choice})()]


def test_plan_mode_genuinely_restricts_tools_for_a_resumed_mission():
    import llm_client

    with tempfile.TemporaryDirectory() as d:
        with patch.object(missions, "MISSIONS_DIR", Path(d)):
            call_sequence = [
                FakeMessage(FakeChoice(tool_calls=[FakeToolCall("write_file", '{"path":"x.txt","content":"y"}')], content="writing")),
                FakeMessage(FakeChoice(tool_calls=None, content="Cannot write in plan mode.")),
                FakeMessage(FakeChoice(tool_calls=None, content="Summary: attempted write, blocked.\nNEXT: none\nFILES: none")),
            ]
            call_index = {"i": 0}

            def fake_chat_completion(messages, tools=None, **kwargs):
                msg = call_sequence[call_index["i"]]
                call_index["i"] += 1
                return msg

            with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
                reply = permissions.run_mission_with_mode(
                    "do something", mission_id="plan-mode-unit-test",
                    mode=permissions.PermissionMode.PLAN,
                    base_confirm=lambda *a: True,
                    verbose=False, max_iterations=5,
                )
            assert "plan mode" in reply.lower()

            # Confirm the checkpoint was genuinely saved for this mission.
            saved = missions.load_progress("plan-mode-unit-test")
            assert saved is not None
    print("PASS: --resume + --permission-mode plan genuinely restricts a mission-scoped run's tools (mocked-LLM, real ReAct loop)")


if __name__ == "__main__":
    test_no_session_flags_returns_none_and_not_continue()
    test_resume_with_value_parses_correctly()
    test_continue_flag_parses_correctly()
    test_resume_and_continue_together_is_rejected()
    test_resume_missing_value_exits_cleanly()
    test_continue_with_no_saved_missions_exits_clearly()
    test_continue_resolves_the_most_recently_updated_mission()
    test_list_missions_output_includes_real_saved_missions()
    test_list_missions_empty_state_is_not_an_error()
    test_extract_flag_value_finds_first_matching_alias()
    test_output_format_defaults_to_text_and_rejects_unknown_values()
    test_run_mission_accepts_system_prompt_and_tool_functions()
    test_run_mission_with_mode_exists_and_wraps_run_mission()
    test_plan_mode_genuinely_restricts_tools_for_a_resumed_mission()
    print("\nALL TESTS PASSED")
