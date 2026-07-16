"""
LIVE end-to-end test of rules.py against a REAL LLM (not mocked):
1. An always-loaded rule ("use pytest, not unittest") -- does the model
   follow it when asked to write a Python test?
2. A path-scoped rule (files under test/scratch/**/*.py must start with a
   specific comment) -- does the model actually add that comment when it
   writes a NEW file under test/scratch/, having seen the rule injected
   as a real-time observation the moment it wrote there?

Run with: PYTHONPATH=/home/user/my-agent python3 test/rules_live_test.py
(requires at least one real provider API key in .env; requires
.agent_rules/testing-conventions.md and .agent_rules/scratch-python-style.md
to exist on disk -- created by this file's setup if missing)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402

RULES_DIR = Path(".agent_rules")
ALWAYS_LOADED_RULE = RULES_DIR / "testing-conventions.md"
PATH_SCOPED_RULE = RULES_DIR / "scratch-python-style.md"

ALWAYS_LOADED_CONTENT = (
    "# Testing Conventions\n\n"
    "Use pytest, not unittest, for all Python tests in this project.\n"
    "Mock all external API calls -- never hit real network endpoints in a test.\n"
)
PATH_SCOPED_CONTENT = (
    "---\npaths: test/scratch/**/*.py\n---\n"
    "Files in test/scratch/ are throwaway scripts. Always add a comment "
    "`# SCRATCH FILE - not part of the permanent codebase` as the first line.\n"
)


def _ensure_rules_exist():
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    ALWAYS_LOADED_RULE.write_text(ALWAYS_LOADED_CONTENT, encoding="utf-8")
    PATH_SCOPED_RULE.write_text(PATH_SCOPED_CONTENT, encoding="utf-8")


def test_always_loaded_rule_influences_real_model_output():
    """The model should write a pytest-style test (not unittest) when
    asked, purely because the rule is in its system prompt -- no explicit
    mention of pytest in the task itself."""
    events = []

    def log(event, payload):
        events.append((event, payload))
        print(f"[{event}] {str(payload)[:200]}")

    reply = agent.run_agent(
        "Write a simple Python test for a function `add(a, b)` that returns a + b. "
        "Show the test code directly in your final answer as a code block, do not "
        "use any file-writing tools.",
        verbose=True, log=log, confirm=lambda *a: True,
        persist_memory=False, max_iterations=6,
    )
    print("\n--- FINAL REPLY ---")
    print(reply)

    uses_pytest = "import pytest" in reply or "def test_" in reply
    uses_unittest = "import unittest" in reply or "unittest.TestCase" in reply
    print(f"\nUses pytest-style: {uses_pytest}")
    print(f"Uses unittest: {uses_unittest}")

    assert uses_pytest, f"expected a pytest-style test per the always-loaded rule, reply:\n{reply}"
    assert not uses_unittest, f"expected the model to AVOID unittest per the rule, reply:\n{reply}"
    print("PASS: the always-loaded rule ('use pytest, not unittest') influenced a real model's output, "
          "with no explicit mention of pytest in the task text itself")


def test_path_scoped_rule_fires_and_influences_a_new_file():
    """Ask the model to write a NEW throwaway script under test/scratch/ --
    the path-scoped rule should fire as a real-time observation the moment
    it writes there, and (if the model follows it) the resulting file
    should contain the required marker comment. Verified against the REAL
    file on disk, not the model's chat summary (see the skills_live_test.py
    lesson from earlier this session about that exact mistake)."""
    events = []

    def log(event, payload):
        events.append((event, payload))
        print(f"[{event}] {str(payload)[:250]}")

    reply = agent.run_agent(
        "Write a small throwaway Python script at test/scratch/rules_live_check.py "
        "that prints 'hello from scratch'. This is just a quick one-off check, not "
        "part of the permanent codebase.",
        verbose=True, log=log, confirm=lambda *a: True,
        persist_memory=False, max_iterations=8,
    )
    print("\n--- FINAL REPLY ---")
    print(reply)

    rule_fired = any(
        e == "Note" and "scratch-python-style" in str(p)
        for e, p in events
    )
    print(f"\nRule fired (visible in event log): {rule_fired}")
    assert rule_fired, (
        f"expected the path-scoped rule to fire as a Note event when the model wrote to "
        f"test/scratch/ -- it didn't. All Note events: {[p for e, p in events if e == 'Note']}"
    )

    target = Path("test/scratch/rules_live_check.py")
    try:
        assert target.exists(), "expected the model to actually create test/scratch/rules_live_check.py"
        content = target.read_text(encoding="utf-8")
        print(f"\n--- REAL WRITTEN FILE ---\n{content}")
        has_marker = "SCRATCH FILE" in content
        print(f"Has required marker comment: {has_marker}")
        assert has_marker, (
            f"the path-scoped rule fired (model saw it), but the real file doesn't contain "
            f"the required marker comment -- content:\n{content}"
        )
        print("PASS: the path-scoped rule fired in real time when the model wrote a matching file, "
              "AND the real file on disk follows the rule's instruction")
    finally:
        if target.exists():
            target.unlink()


if __name__ == "__main__":
    _ensure_rules_exist()
    test_always_loaded_rule_influences_real_model_output()
    print("=" * 70)
    test_path_scoped_rule_fires_and_influences_a_new_file()
    print("\nALL LIVE TESTS PASSED")
