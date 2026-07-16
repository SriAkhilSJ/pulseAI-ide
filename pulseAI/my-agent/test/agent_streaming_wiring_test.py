"""
Direct test that agent.run_agent()'s new on_command_line parameter
actually reaches tools.run_command()'s streaming path end-to-end through
the real dispatch chain (run_agent -> _run_tool_calls -> _dispatch_tool_call
-> tools.run_command), not just that tools.run_command() itself streams in
isolation (already covered by test/run_command_streaming_test.py).

Bypasses the LLM entirely by calling _dispatch_tool_call directly with a
synthetic run_command call -- this isolates the WIRING bug class (an
agent.py parameter silently not reaching the tool) from LLM
non-determinism, which is the right test for this specific change.

Run with: PYTHONPATH=/home/user/my-agent python3 test/agent_streaming_wiring_test.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


def test_dispatch_tool_call_forwards_on_command_line():
    lines = []
    timestamps = []
    start = time.monotonic()

    def on_line(line):
        lines.append(line)
        timestamps.append(time.monotonic() - start)

    result = agent._dispatch_tool_call(
        "run_command",
        '{"cmd": "for i in 1 2 3; do echo line-$i; sleep 0.3; done", "timeout": 10}',
        confirm=lambda *a: True,
        on_command_line=on_line,
    )
    print("result:", result)
    print("lines:", lines)
    print("timestamps:", [round(t, 2) for t in timestamps])

    assert lines == ["line-1", "line-2", "line-3"]
    assert timestamps[-1] - timestamps[0] > 0.4, "lines should be spread out in real time, not delivered in a burst"
    print("PASS: _dispatch_tool_call forwards on_command_line to a real run_command call, streaming genuinely")


def test_other_tools_ignore_on_command_line():
    """A non-run_command tool call must not break or misbehave just
    because on_command_line was set for the whole dispatch."""
    calls = []
    result = agent._dispatch_tool_call(
        "list_files",
        '{"directory": "."}',
        confirm=lambda *a: True,
        on_command_line=calls.append,
    )
    assert not result.startswith("ERROR")
    assert calls == [], "on_command_line must never be invoked for a non-run_command tool"
    print("PASS: on_command_line is silently ignored for tools other than run_command")


def test_run_tool_calls_forwards_on_command_line():
    """One level up: _run_tool_calls (the real per-turn dispatcher
    run_agent's ReAct loop actually calls) must also forward it."""
    from cache import ToolCache

    class FakeToolCall:
        def __init__(self, name, arguments):
            class F:
                pass
            self.function = F()
            self.function.name = name
            self.function.arguments = arguments
            self.id = "fake_1"

    lines = []
    tc = FakeToolCall("run_command", '{"cmd": "echo one; echo two", "timeout": 5}')
    results = agent._run_tool_calls(
        [tc], confirm=lambda *a: True, cache=ToolCache(), log=lambda *a: None,
        on_command_line=lines.append,
    )
    result, _ = results[0]
    print("result:", result)
    assert "one" in result and "two" in result
    assert lines == ["one", "two"]
    print("PASS: _run_tool_calls forwards on_command_line down to the real tool call")


if __name__ == "__main__":
    test_dispatch_tool_call_forwards_on_command_line()
    test_other_tools_ignore_on_command_line()
    test_run_tool_calls_forwards_on_command_line()
    print("\nALL TESTS PASSED")
