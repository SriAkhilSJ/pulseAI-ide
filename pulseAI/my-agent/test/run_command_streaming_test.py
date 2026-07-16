"""
Direct test of run_command's new on_line streaming callback (tools.py).

Proves the ACTUAL requirement from the original request: lines must
arrive incrementally as the command runs, not all at once after it
finishes -- verified by measuring wall-clock time between callback
invocations, not just checking the final concatenated output is correct.

Also verifies:
  - on_line=None (the default, and the only path the LLM's own tool call
    can ever take, since on_line isn't a TOOL_SPECS parameter) behaves
    EXACTLY as before this feature was added.
  - A broken/raising on_line callback never kills the command it's
    watching (matches the docstring's stated guarantee).
  - Timeout still works correctly in the streaming path, including
    reporting partial output collected before the kill.

Run with: PYTHONPATH=/home/user/my-agent python3 test/run_command_streaming_test.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


def test_default_behavior_unchanged():
    """on_line=None must behave exactly like the pre-existing run_command."""
    result = tools.run_command("echo hello")
    print(result)
    assert result.startswith("exit_code=0")
    assert "hello" in result
    print("PASS: on_line=None (default) behaves exactly as before")


def test_lines_arrive_incrementally_not_all_at_once():
    """
    THE core requirement: verify lines appear one by one in real time,
    not buffered and delivered all at once after the command finishes.
    Measured directly via timestamps recorded inside the callback.
    """
    timestamps = []
    lines_seen = []

    def on_line(line):
        timestamps.append(time.monotonic())
        lines_seen.append(line)

    start = time.monotonic()
    result = tools.run_command(
        "for i in 1 2 3 4 5; do echo step $i; sleep 0.4; done",
        timeout=10,
        on_line=on_line,
    )
    total_elapsed = time.monotonic() - start

    print("result:", result)
    print("lines_seen:", lines_seen)
    print("timestamps (relative to start):", [round(t - start, 2) for t in timestamps])

    assert len(lines_seen) == 5, f"expected 5 lines, got {len(lines_seen)}"
    assert lines_seen == ["step 1", "step 2", "step 3", "step 4", "step 5"]

    # The critical assertion: if streaming were fake (i.e. lines delivered
    # all at once at the end), all 5 timestamps would cluster within a few
    # milliseconds of each other, near total_elapsed. Real streaming means
    # they're spread out across the ~2s runtime (5 * 0.4s sleeps).
    spread = timestamps[-1] - timestamps[0]
    assert spread > 1.0, (
        f"lines arrived within {spread:.3f}s of each other -- this looks like "
        f"buffered/batched delivery at the end, not real incremental streaming"
    )
    # And the FIRST line must have arrived well before the command finished
    # (not held back and delivered in a final burst).
    first_line_delay = timestamps[0] - start
    assert first_line_delay < (total_elapsed - 0.5), (
        f"first line arrived at {first_line_delay:.2f}s but the command took "
        f"{total_elapsed:.2f}s total -- looks like it was delayed until near the end"
    )
    print(f"PASS: lines genuinely streamed incrementally (spread={spread:.2f}s across {total_elapsed:.2f}s total runtime)")


def test_broken_callback_does_not_kill_command():
    def bad_on_line(line):
        raise RuntimeError("simulated broken UI callback")

    result = tools.run_command("echo one; echo two; echo three", timeout=10, on_line=bad_on_line)
    print(result)
    assert result.startswith("exit_code=0")
    assert "one" in result and "two" in result and "three" in result
    print("PASS: a raising on_line callback does not kill or corrupt the command's real execution")


def test_timeout_still_works_in_streaming_path():
    lines = []
    start = time.monotonic()
    result = tools.run_command(
        "echo start; sleep 5; echo end",
        timeout=1,
        on_line=lines.append,
    )
    elapsed = time.monotonic() - start
    print(result)
    print(f"elapsed: {elapsed:.2f}s")
    assert result.startswith("ERROR: command timed out after 1s")
    assert "start" in result, "partial output before the timeout should still be reported"
    assert "end" not in result, "the command should have been killed before reaching 'end'"
    assert elapsed < 3, f"should time out close to the requested 1s, took {elapsed:.2f}s"
    print("PASS: timeout still works in the streaming path, with partial output reported")


def test_sensitive_command_still_blocked_with_streaming():
    result = tools.run_command("cat .env", on_line=lambda l: None)
    print(result)
    assert result.startswith("ERROR") and "sensitive" in result.lower()
    print("PASS: sensitive-command blocking is unaffected by on_line being set")


def test_llm_tool_spec_cannot_set_on_line():
    """Confirms the docstring's safety claim: on_line is not a parameter
    the LLM's tool call can ever populate."""
    spec = next(s for s in tools.TOOL_SPECS if s["function"]["name"] == "run_command")
    params = spec["function"]["parameters"]["properties"]
    assert "on_line" not in params, "on_line must never be exposed as an LLM-settable tool parameter"
    print("PASS: run_command's TOOL_SPECS schema has no on_line parameter -- the LLM can never set it")


if __name__ == "__main__":
    test_default_behavior_unchanged()
    test_lines_arrive_incrementally_not_all_at_once()
    test_broken_callback_does_not_kill_command()
    test_timeout_still_works_in_streaming_path()
    test_sensitive_command_still_blocked_with_streaming()
    test_llm_tool_spec_cannot_set_on_line()
    print("\nALL TESTS PASSED")
