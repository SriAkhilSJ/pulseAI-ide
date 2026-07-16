"""
Direct tests for main.py's --permission-mode CLI parsing and its wiring
into main()'s call path -- WITHOUT running the actual interactive REPL
loop or calling any real LLM (isolates argv-parsing/dispatch correctness
from the ReAct loop itself, same philosophy as test/permissions_test.py).

Run with: PYTHONPATH=/home/user/my-agent python3 test/main_permission_mode_cli_test.py
"""
import io
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import permissions  # noqa: E402


def test_no_flag_returns_none_not_default_enum():
    """Critical distinction: omitting --permission-mode entirely must
    return Python None (meaning 'take the exact prior code path'), NOT
    PermissionMode.DEFAULT -- see _parse_permission_mode's own docstring
    for why these are deliberately different signals."""
    result = main._parse_permission_mode([])
    assert result is None, f"expected None for no flag at all, got: {result}"
    result2 = main._parse_permission_mode(["some request text"])
    assert result2 is None
    print("PASS: no --permission-mode flag returns None (not PermissionMode.DEFAULT)")


def test_valid_mode_values_all_parse_correctly():
    for mode in permissions.PermissionMode:
        result = main._parse_permission_mode(["--permission-mode", mode.value])
        assert result == mode, f"expected {mode} for value '{mode.value}', got {result}"
    print("PASS: every valid PermissionMode value parses to the correct enum member")


def test_missing_value_after_flag_exits_with_error():
    with mock.patch("sys.exit") as fake_exit, mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
        main._parse_permission_mode(["--permission-mode"])
        fake_exit.assert_called_once_with(1)
        assert "requires a value" in fake_err.getvalue()
    print("PASS: --permission-mode with no following value exits(1) with a clear error, doesn't crash")


def test_unknown_mode_value_exits_with_error_listing_valid_ones():
    with mock.patch("sys.exit") as fake_exit, mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
        main._parse_permission_mode(["--permission-mode", "nonexistent-mode"])
        fake_exit.assert_called_once_with(1)
        stderr_text = fake_err.getvalue()
        assert "unknown --permission-mode value" in stderr_text
        assert "plan" in stderr_text  # the valid-values list should be printed
    print("PASS: an invalid mode value exits(1) with a clear error listing valid values")


def test_dry_run_warning_fires_for_bypass_and_accept_edits_and_auto():
    for mode in (permissions.PermissionMode.BYPASS, permissions.PermissionMode.ACCEPT_EDITS, permissions.PermissionMode.AUTO):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            main._warn_about_dry_run_mode_interaction(mode)
            output = fake_err.getvalue()
            assert output, f"expected a warning to be printed for {mode}, got nothing"
    print("PASS: combining --dry-run with bypass/accept_edits/auto prints an explicit warning about reduced protection")


def test_dry_run_warning_silent_for_default_plan_dont_ask():
    for mode in (permissions.PermissionMode.DEFAULT, permissions.PermissionMode.PLAN, permissions.PermissionMode.DONT_ASK):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            main._warn_about_dry_run_mode_interaction(mode)
            output = fake_err.getvalue()
            assert output == "", f"expected NO warning for {mode} (its confirm/registry behavior doesn't reduce --dry-run's protection), got: {output!r}"
    print("PASS: combining --dry-run with default/plan/dont_ask prints no warning (correctly, since --dry-run's protection is unaffected)")


def test_main_takes_the_exact_prior_call_path_when_no_mode_given():
    """The core backward-compatibility guarantee: with no --permission-mode
    flag, main()'s single REPL turn must call agent.run_agent directly
    (NOT permissions.run_agent_with_mode) -- proven by patching BOTH and
    confirming only the plain one is invoked, feeding one line of input
    then EOF to end the loop immediately."""
    with mock.patch.object(sys, "argv", ["main.py"]), \
         mock.patch("builtins.input", side_effect=["hello", EOFError()]), \
         mock.patch("main.run_agent", return_value="fake reply") as fake_run_agent, \
         mock.patch("permissions.run_agent_with_mode") as fake_run_agent_with_mode, \
         mock.patch("process_manager.cleanup_orphans_from_previous_run", return_value="nothing to clean up"), \
         mock.patch("process_manager.cleanup_all"), \
         mock.patch("atexit.register"):
        main.main()

    fake_run_agent.assert_called_once()
    fake_run_agent_with_mode.assert_not_called()
    print("PASS: with no --permission-mode flag, main() calls the exact prior agent.run_agent path, never permissions.run_agent_with_mode")


def test_main_routes_through_run_agent_with_mode_when_flag_given():
    """The other half: WITH --permission-mode, main() must call
    permissions.run_agent_with_mode (not the plain run_agent), passing the
    parsed mode through correctly."""
    with mock.patch.object(sys, "argv", ["main.py", "--permission-mode", "plan"]), \
         mock.patch("builtins.input", side_effect=["hello", EOFError()]), \
         mock.patch("main.run_agent") as fake_run_agent, \
         mock.patch("permissions.run_agent_with_mode", return_value="fake reply") as fake_run_agent_with_mode, \
         mock.patch("process_manager.cleanup_orphans_from_previous_run", return_value="nothing to clean up"), \
         mock.patch("process_manager.cleanup_all"), \
         mock.patch("atexit.register"):
        main.main()

    fake_run_agent.assert_not_called()
    fake_run_agent_with_mode.assert_called_once()
    _, kwargs = fake_run_agent_with_mode.call_args
    assert kwargs["mode"] == permissions.PermissionMode.PLAN
    print("PASS: with --permission-mode plan, main() routes through permissions.run_agent_with_mode with mode=PLAN")


def test_dry_run_confirm_still_passed_as_base_confirm_when_combined():
    """--dry-run's confirm callable must still be threaded through as
    base_confirm even when a mode is also active -- otherwise combining
    the two flags would silently drop --dry-run's behavior entirely for
    modes that DO consult base_confirm (default/plan/dont_ask -- covered
    above -- and the gated parts of accept_edits/auto/bypass)."""
    with mock.patch.object(sys, "argv", ["main.py", "--permission-mode", "auto", "--dry-run"]), \
         mock.patch("builtins.input", side_effect=["hello", EOFError()]), \
         mock.patch("permissions.run_agent_with_mode", return_value="fake reply") as fake_run_agent_with_mode, \
         mock.patch("process_manager.cleanup_orphans_from_previous_run", return_value="nothing to clean up"), \
         mock.patch("process_manager.cleanup_all"), \
         mock.patch("atexit.register"):
        main.main()

    _, kwargs = fake_run_agent_with_mode.call_args
    assert kwargs["base_confirm"] is main._dry_run_confirm, (
        f"expected base_confirm=main._dry_run_confirm when --dry-run is combined with a mode, got: {kwargs['base_confirm']}"
    )
    print("PASS: --dry-run's confirm callable is still passed as base_confirm even when combined with --permission-mode")


if __name__ == "__main__":
    test_no_flag_returns_none_not_default_enum()
    test_valid_mode_values_all_parse_correctly()
    test_missing_value_after_flag_exits_with_error()
    test_unknown_mode_value_exits_with_error_listing_valid_ones()
    test_dry_run_warning_fires_for_bypass_and_accept_edits_and_auto()
    test_dry_run_warning_silent_for_default_plan_dont_ask()
    test_main_takes_the_exact_prior_call_path_when_no_mode_given()
    test_main_routes_through_run_agent_with_mode_when_flag_given()
    test_dry_run_confirm_still_passed_as_base_confirm_when_combined()
    print("\nALL TESTS PASSED")
