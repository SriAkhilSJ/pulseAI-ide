"""
Direct wiring test for rules.py's path-scoped injection through the REAL
agent.py dispatch chain -- WITHOUT calling any real LLM (a fully mocked
llm_client.chat_completion drives the ReAct loop through a scripted
sequence of tool calls). Isolates the WIRING bug class (does agent.py's
loop actually check/inject path-scoped rules correctly) from LLM
non-determinism, same philosophy as test/agent_streaming_wiring_test.py
and test/batching_nudge_test.py's own wiring test.

A separate live test (test/rules_live_test.py) exercises this with a real
LLM actually choosing to touch a matching file on its own.

Run with: PYTHONPATH=/home/user/my-agent python3 test/rules_wiring_test.py
"""
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
import agent  # noqa: E402
import llm_client  # noqa: E402
import rules  # noqa: E402

RULES_DIR = rules._rules_dir()


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments, id="fake"):
        self.function = FakeFunction(name, arguments)
        self.id = id


class FakeChoice:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


class FakeMessage:
    def __init__(self, choice):
        self.choices = [type("C", (), {"message": choice})()]


def _setup_rule(name: str, content: str):
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    (RULES_DIR / f"{name}.md").write_text(content, encoding="utf-8")


def _cleanup_rule(name: str):
    p = RULES_DIR / f"{name}.md"
    if p.exists():
        p.unlink()


def test_path_scoped_rule_fires_on_matching_read_file_call():
    """The core wiring test: a rule scoped to test/scratch/**/*.py should
    fire (its body injected into the tool-result content) the first time
    the scripted sequence reads a matching file, using a REAL target file
    on disk (read_file needs the file to actually exist to succeed)."""
    _setup_rule("scratch-py-rule", "---\npaths: test/scratch/**/*.py\n---\nUNIQUE_RULE_MARKER_ABC123")
    target = Path("test/scratch/rules_wiring_target.py")
    target.write_text("print('hello')\n", encoding="utf-8")

    tool_call_sequence = [
        [FakeToolCall("read_file", json.dumps({"path": str(target)}), id="c1")],
        None,  # second turn: no tool calls -> task complete
    ]
    call_index = {"i": 0}

    def fake_chat_completion(messages, tools=None, **kwargs):
        tc_list = tool_call_sequence[call_index["i"]]
        call_index["i"] += 1
        if tc_list is None:
            return FakeMessage(FakeChoice(tool_calls=None, content="done"))
        return FakeMessage(FakeChoice(tool_calls=tc_list, content=None))

    captured_tool_messages = []
    real_run_tool_calls = agent._run_tool_calls

    def spying_run_tool_calls(*args, **kwargs):
        results = real_run_tool_calls(*args, **kwargs)
        return results

    try:
        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            reply = agent.run_agent(
                "irrelevant task text",
                verbose=False, log=lambda *a: None, confirm=lambda *a: True,
                persist_memory=False, max_iterations=5,
            )
        # Inspect what got fed back to the (fake) LLM on the SECOND call --
        # that's the messages list containing the tool-result content with
        # the rule injected, if the wiring worked.
        assert reply == "done"
    finally:
        _cleanup_rule("scratch-py-rule")
        target.unlink()

    print("PASS: run_agent completed a scripted read_file call against a path-scoped rule's target without crashing")


def test_path_scoped_rule_actually_appears_in_tool_result_content():
    """More precise version: directly capture the exact messages list
    run_agent builds, to confirm the rule's body genuinely appears in the
    tool-result content for the matching call."""
    _setup_rule("scratch-py-rule2", "---\npaths: test/scratch/**/*.py\n---\nUNIQUE_RULE_MARKER_XYZ789")
    target = Path("test/scratch/rules_wiring_target2.py")
    target.write_text("print('hello')\n", encoding="utf-8")

    captured = {"messages": None}

    tool_call_sequence = [
        [FakeToolCall("read_file", json.dumps({"path": str(target)}), id="c1")],
        None,
    ]
    call_index = {"i": 0}

    def fake_chat_completion(messages, tools=None, **kwargs):
        captured["messages"] = messages  # capture on EVERY call; the last one before "done" has what we need
        tc_list = tool_call_sequence[call_index["i"]]
        call_index["i"] += 1
        if tc_list is None:
            return FakeMessage(FakeChoice(tool_calls=None, content="done"))
        return FakeMessage(FakeChoice(tool_calls=tc_list, content=None))

    try:
        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            agent.run_agent(
                "irrelevant task text",
                verbose=False, log=lambda *a: None, confirm=lambda *a: True,
                persist_memory=False, max_iterations=5,
            )
        tool_messages = [m for m in captured["messages"] if m["role"] == "tool"]
        assert tool_messages, "expected at least one tool-result message"
        assert any("UNIQUE_RULE_MARKER_XYZ789" in m["content"] for m in tool_messages), (
            f"expected the rule's body to appear in a tool-result message, got: {tool_messages}"
        )
        print("PASS: the path-scoped rule's body genuinely appears in the tool-result content for the matching read_file call")
    finally:
        _cleanup_rule("scratch-py-rule2")
        target.unlink()


def test_path_scoped_rule_does_not_fire_for_non_matching_path():
    """A rule scoped to test/scratch/**/*.py must NOT fire for a .txt file
    read in the same directory."""
    _setup_rule("scratch-py-rule3", "---\npaths: test/scratch/**/*.py\n---\nUNIQUE_RULE_MARKER_SHOULD_NOT_APPEAR")
    target = Path("test/scratch/rules_wiring_target3.txt")
    target.write_text("not python\n", encoding="utf-8")

    captured = {"messages": None}
    tool_call_sequence = [
        [FakeToolCall("read_file", json.dumps({"path": str(target)}), id="c1")],
        None,
    ]
    call_index = {"i": 0}

    def fake_chat_completion(messages, tools=None, **kwargs):
        captured["messages"] = messages
        tc_list = tool_call_sequence[call_index["i"]]
        call_index["i"] += 1
        if tc_list is None:
            return FakeMessage(FakeChoice(tool_calls=None, content="done"))
        return FakeMessage(FakeChoice(tool_calls=tc_list, content=None))

    try:
        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            agent.run_agent(
                "irrelevant task text",
                verbose=False, log=lambda *a: None, confirm=lambda *a: True,
                persist_memory=False, max_iterations=5,
            )
        tool_messages = [m for m in captured["messages"] if m["role"] == "tool"]
        assert not any("UNIQUE_RULE_MARKER_SHOULD_NOT_APPEAR" in m["content"] for m in tool_messages), (
            "a .py-scoped rule must NOT fire for a .txt file read"
        )
        print("PASS: a path-scoped rule correctly does NOT fire for a non-matching file extension")
    finally:
        _cleanup_rule("scratch-py-rule3")
        target.unlink()


def test_path_scoped_rule_fires_only_once_per_task_even_with_repeated_matches():
    """A rule matching 2 different files in the SAME task should only
    inject its body once (fired_rule_names dedup), not once per matching
    call -- avoids bloating context with duplicate rule text."""
    _setup_rule("scratch-py-rule4", "---\npaths: test/scratch/**/*.py\n---\nUNIQUE_RULE_MARKER_ONCE_ONLY")
    target1 = Path("test/scratch/rules_wiring_target4a.py")
    target2 = Path("test/scratch/rules_wiring_target4b.py")
    target1.write_text("print('a')\n", encoding="utf-8")
    target2.write_text("print('b')\n", encoding="utf-8")

    captured = {"messages": None}
    tool_call_sequence = [
        [FakeToolCall("read_file", json.dumps({"path": str(target1)}), id="c1")],
        [FakeToolCall("read_file", json.dumps({"path": str(target2)}), id="c2")],
        None,
    ]
    call_index = {"i": 0}

    def fake_chat_completion(messages, tools=None, **kwargs):
        captured["messages"] = messages
        tc_list = tool_call_sequence[call_index["i"]]
        call_index["i"] += 1
        if tc_list is None:
            return FakeMessage(FakeChoice(tool_calls=None, content="done"))
        return FakeMessage(FakeChoice(tool_calls=tc_list, content=None))

    try:
        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            agent.run_agent(
                "irrelevant task text",
                verbose=False, log=lambda *a: None, confirm=lambda *a: True,
                persist_memory=False, max_iterations=5,
            )
        all_content = "\n".join(
            m["content"] for m in captured["messages"] if m["role"] == "tool"
        )
        occurrence_count = all_content.count("UNIQUE_RULE_MARKER_ONCE_ONLY")
        assert occurrence_count == 1, (
            f"expected the rule to fire exactly ONCE across the whole task even with 2 matching "
            f"reads, got {occurrence_count} occurrences"
        )
        print("PASS: a path-scoped rule fires exactly once per task, even with multiple matching file touches")
    finally:
        _cleanup_rule("scratch-py-rule4")
        target1.unlink()
        target2.unlink()


def test_always_loaded_rule_appears_in_system_prompt_for_a_real_run():
    """The always-loaded half of rules.py, verified the same way skills.py's
    metadata-block test was: directly inspect the actual system prompt
    run_agent() sends to the (fake) LLM."""
    _setup_rule("always-on-rule", "# Always On\nUNIQUE_ALWAYS_LOADED_MARKER_DEF456")

    captured = {"messages": None}

    def fake_chat_completion(messages, tools=None, **kwargs):
        captured["messages"] = messages
        return FakeMessage(FakeChoice(tool_calls=None, content="done"))

    try:
        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            agent.run_agent(
                "irrelevant task text",
                verbose=False, log=lambda *a: None, confirm=lambda *a: True,
                persist_memory=False, max_iterations=2,
            )
        system_message = next(m for m in captured["messages"] if m["role"] == "system")
        assert "UNIQUE_ALWAYS_LOADED_MARKER_DEF456" in system_message["content"]
        print("PASS: an always-loaded rule (no paths: frontmatter) appears in the system prompt from the very first turn")
    finally:
        _cleanup_rule("always-on-rule")


if __name__ == "__main__":
    test_path_scoped_rule_fires_on_matching_read_file_call()
    test_path_scoped_rule_actually_appears_in_tool_result_content()
    test_path_scoped_rule_does_not_fire_for_non_matching_path()
    test_path_scoped_rule_fires_only_once_per_task_even_with_repeated_matches()
    test_always_loaded_rule_appears_in_system_prompt_for_a_real_run()
    print("\nALL TESTS PASSED")
