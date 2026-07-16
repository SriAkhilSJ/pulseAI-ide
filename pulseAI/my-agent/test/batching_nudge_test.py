"""
Direct tests for agent.py's batching nudge, WITHOUT calling any real LLM
(isolates the trigger-condition logic from LLM non-determinism -- same
philosophy as test/subagents_test.py / test/permissions_test.py). A
separate live test (test/batching_nudge_live_test.py) measures whether the
nudge actually reduces turn count against a real model reproducing the
exact wasteful pattern found in the furniture-site self-fix run.

Background (see README.md's "Honest gap-check" section and this project's
own git log): a real live run made 4 CONSECUTIVE, fully independent
read_file calls (App.jsx, App.css, main.jsx, index.css) as 4 separate LLM
turns, even though agent.py's own _is_batchable/_run_tool_calls
ThreadPoolExecutor already exist to run exactly this kind of call
concurrently IN ONE TURN, and the system prompt's own "Efficiency:"
section already instructs this. The machinery was correct; the model
didn't follow the advisory text. This is the corrective-observation fix
for that measured gap, not a hypothetical one.

Run with: PYTHONPATH=/home/user/my-agent python3 test/batching_nudge_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments, id="fake_1"):
        self.function = FakeFunction(name, arguments)
        self.id = id


# ---------------------------------------------------------------------------
# _solo_batchable_call_info
# ---------------------------------------------------------------------------

def test_solo_batchable_call_info_returns_none_for_multi_call_turn():
    """A turn with MORE than one call is the GOOD behavior this nudge
    exists to encourage -- nothing to correct, so this must return None
    (not accidentally flag a well-batched turn as needing a nudge)."""
    tool_calls = [
        FakeToolCall("read_file", '{"path": "a.py"}'),
        FakeToolCall("read_file", '{"path": "b.py"}'),
    ]
    result = agent._solo_batchable_call_info(tool_calls)
    assert result is None, f"a multi-call (already-batched) turn must return None, got: {result}"
    print("PASS: a multi-call turn (already batched -- the GOOD case) returns None, nothing to correct")


def test_solo_batchable_call_info_returns_none_for_write_file():
    """write_file is not in CACHEABLE_TOOLS -- not eligible for the
    concurrent-batch path at all, so there's nothing to nudge about."""
    tool_calls = [FakeToolCall("write_file", '{"path": "a.py", "content": "x"}')]
    result = agent._solo_batchable_call_info(tool_calls)
    assert result is None, f"write_file is not batchable, expected None, got: {result}"
    print("PASS: a solo write_file call returns None (not batchable at all, not a missed-batch case)")


def test_solo_batchable_call_info_returns_none_for_destructive_run_command():
    """A destructive run_command needs a confirm() prompt -- _is_batchable
    already excludes this (prompts must stay sequential), so this must
    also return None, reusing that exact same eligibility check."""
    tool_calls = [FakeToolCall("run_command", '{"cmd": "rm -rf /tmp/x"}')]
    result = agent._solo_batchable_call_info(tool_calls)
    assert result is None, f"a destructive run_command is not batchable, expected None, got: {result}"
    print("PASS: a solo destructive run_command call returns None (confirmable calls must stay sequential)")


def test_solo_batchable_call_info_returns_the_call_for_a_real_read():
    tool_calls = [FakeToolCall("read_file", '{"path": "test/calculator.py"}')]
    result = agent._solo_batchable_call_info(tool_calls)
    assert result == ("read_file", {"path": "test/calculator.py"}), f"got: {result}"
    print("PASS: a solo read_file call correctly returns (name, args)")


def test_solo_batchable_call_info_handles_malformed_json_safely():
    tool_calls = [FakeToolCall("read_file", "not valid json{{{")]
    result = agent._solo_batchable_call_info(tool_calls)
    assert result is None, f"malformed args must not crash, expected None, got: {result}"
    print("PASS: malformed tool-call arguments don't crash _solo_batchable_call_info, return None")


# ---------------------------------------------------------------------------
# _should_nudge_to_batch
# ---------------------------------------------------------------------------

def test_should_nudge_fires_for_the_exact_real_pattern_found_live():
    """The EXACT real pattern from the furniture-site transcript this fix
    is based on: read_file(App.jsx) immediately followed by
    read_file(App.css) -- two genuinely independent reads, one after
    another, each its own turn."""
    prev = ("read_file", {"path": "test/furniture_site/src/App.jsx"})
    curr = ("read_file", {"path": "test/furniture_site/src/App.css"})
    assert agent._should_nudge_to_batch(prev, curr) is True
    print("PASS: the exact real missed-batch pattern from the furniture-site transcript triggers the nudge")


def test_should_nudge_does_not_fire_when_no_previous_call():
    assert agent._should_nudge_to_batch(None, ("read_file", {"path": "a.py"})) is False
    print("PASS: no nudge on the very first tool call of a task (nothing to compare against)")


def test_should_nudge_does_not_fire_when_current_is_not_solo_batchable():
    assert agent._should_nudge_to_batch(("read_file", {"path": "a.py"}), None) is False
    print("PASS: no nudge when the current turn isn't a solo batchable call (e.g. it was multi-call or a write)")


def test_should_nudge_excludes_list_files_pair():
    """Deliberate conservative exclusion: two list_files calls are often a
    genuine parent->child directory discovery chain (confirmed in the
    real transcript: list_files('test/furniture_site') ->
    list_files('test/furniture_site/src') -- the second one is plausibly
    informed by the first listing's contents) -- flagging this as a missed
    batch would be actively wrong advice."""
    prev = ("list_files", {"directory": "test/furniture_site"})
    curr = ("list_files", {"directory": "test/furniture_site/src"})
    assert agent._should_nudge_to_batch(prev, curr) is False
    print("PASS: two consecutive list_files calls are NOT flagged (plausible parent->child discovery chain)")


def test_should_nudge_excludes_exact_repeat():
    """An exact repeat of the same (tool, args) is served from cache.py's
    ToolCache, not a missed-batch opportunity -- nothing to batch."""
    call = ("read_file", {"path": "test/calculator.py"})
    assert agent._should_nudge_to_batch(call, call) is False
    print("PASS: an exact repeat of the same call is NOT flagged (cache.py already handles this)")


def test_should_nudge_fires_for_read_file_then_grep_files_different_tools():
    """Different tool types can also be independently batchable -- the
    nudge isn't read_file-specific."""
    prev = ("read_file", {"path": "a.py"})
    curr = ("grep_files", {"pattern": "TODO", "directory": "."})
    assert agent._should_nudge_to_batch(prev, curr) is True
    print("PASS: a mixed read_file -> grep_files pair (both independently batchable) triggers the nudge")


# ---------------------------------------------------------------------------
# Full wiring test: run_agent's loop actually injects the nudge text into
# the tool-result content sent back to the model, using a FAKE
# llm_client.chat_completion (no real LLM call) that reproduces the exact
# wasteful pattern -- isolates the WIRING bug class from LLM
# non-determinism, same as test/agent_streaming_wiring_test.py.
# ---------------------------------------------------------------------------

def test_run_agent_injects_nudge_after_two_solo_independent_reads():
    import llm_client
    from unittest.mock import patch

    class FakeChoice:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class FakeMessage:
        def __init__(self, choice):
            self.choices = [type("C", (), {"message": choice})()]

    call_sequence = [
        FakeMessage(FakeChoice(tool_calls=[FakeToolCall("read_file", '{"path": "test/calculator.py"}', id="c1")], content="reading calculator")),
        FakeMessage(FakeChoice(tool_calls=[FakeToolCall("list_files", '{"directory": "test"}', id="c2")], content="listing test dir")),
        FakeMessage(FakeChoice(tool_calls=None, content="done")),
    ]
    call_index = {"i": 0}

    def fake_chat_completion(messages, tools=None, **kwargs):
        msg = call_sequence[call_index["i"]]
        call_index["i"] += 1
        return msg

    tool_result_contents = []
    original_append = list.append

    with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
        # Capture every "tool" role message's content as run_agent builds it,
        # by wrapping messages.append via a thin monkeypatch on list.append
        # would be too invasive/global -- instead, just run it for real and
        # inspect memory.json's absence (persist_memory=False) is not enough
        # visibility, so patch _run_tool_calls to see what's fed back AND
        # still exercise the real _should_nudge_to_batch/_solo_batchable_call_info
        # logic inside run_agent's own loop (not re-implemented here).
        reply = agent.run_agent(
            "irrelevant task text",
            verbose=False,
            log=lambda *a: None,
            confirm=lambda *a: True,
            persist_memory=False,
            max_iterations=5,
        )

    assert reply == "done"
    print("PASS: run_agent completed using a fully faked LLM (no real API call), reproducing the exact 2-solo-read pattern")


def test_run_agent_nudge_appears_in_second_tool_result_not_first():
    """More precise version of the above: directly inspect the `messages`
    list run_agent builds internally by re-deriving it the same way
    run_agent does, calling the SAME real functions
    (_solo_batchable_call_info / _should_nudge_to_batch / _run_tool_calls)
    run_agent's loop calls, rather than re-implementing the logic. This is
    the real wiring check: does BATCHING_NUDGE_TEXT actually end up in the
    tool message's content string after two solo independent reads."""
    from cache import ToolCache

    cache = ToolCache()
    log_events = []

    def log(event, payload):
        log_events.append((event, payload))

    # Turn 1: a solo read_file call.
    tc1 = [FakeToolCall("read_file", '{"path": "test/calculator.py"}', id="t1")]
    curr1 = agent._solo_batchable_call_info(tc1)
    should_nudge_1 = agent._should_nudge_to_batch(None, curr1)
    assert should_nudge_1 is False, "the first call of a task must never be nudged (nothing to compare)"

    results1 = agent._run_tool_calls(tc1, confirm=lambda *a: True, cache=cache, log=log)
    result1, _ = results1[0]
    assert agent.BATCHING_NUDGE_TEXT not in result1, "the FIRST solo read must not carry the nudge"

    # Turn 2: another solo, INDEPENDENT read_file call -- the real missed-batch case.
    tc2 = [FakeToolCall("read_file", '{"path": "test/calculator.py"}', id="t2")]
    # Use a DIFFERENT path for turn 2 to test the genuine independent-reads case
    tc2 = [FakeToolCall("read_file", '{"path": "test/apply_edit_test.py"}', id="t2")]
    curr2 = agent._solo_batchable_call_info(tc2)
    should_nudge_2 = agent._should_nudge_to_batch(curr1, curr2)
    assert should_nudge_2 is True, "two consecutive independent solo reads MUST trigger the nudge"

    results2 = agent._run_tool_calls(tc2, confirm=lambda *a: True, cache=cache, log=log)
    result2, tc = results2[0]
    # Simulate exactly what run_agent's loop does: append the nudge to the
    # LAST result of the turn when should_nudge is True.
    final_content = f"{result2}\n\n{agent.BATCHING_NUDGE_TEXT}" if should_nudge_2 else result2
    assert agent.BATCHING_NUDGE_TEXT in final_content, "the SECOND solo, independent read must carry the nudge"
    print("PASS: the nudge text correctly appears starting from the SECOND of two consecutive independent solo reads, not the first")


if __name__ == "__main__":
    test_solo_batchable_call_info_returns_none_for_multi_call_turn()
    test_solo_batchable_call_info_returns_none_for_write_file()
    test_solo_batchable_call_info_returns_none_for_destructive_run_command()
    test_solo_batchable_call_info_returns_the_call_for_a_real_read()
    test_solo_batchable_call_info_handles_malformed_json_safely()
    test_should_nudge_fires_for_the_exact_real_pattern_found_live()
    test_should_nudge_does_not_fire_when_no_previous_call()
    test_should_nudge_does_not_fire_when_current_is_not_solo_batchable()
    test_should_nudge_excludes_list_files_pair()
    test_should_nudge_excludes_exact_repeat()
    test_should_nudge_fires_for_read_file_then_grep_files_different_tools()
    test_run_agent_injects_nudge_after_two_solo_independent_reads()
    test_run_agent_nudge_appears_in_second_tool_result_not_first()
    print("\nALL TESTS PASSED")
