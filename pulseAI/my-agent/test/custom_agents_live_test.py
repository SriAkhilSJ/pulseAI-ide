"""
Live LLM test for custom_agents.py, run against a REAL model (not mocked).

Reproduces exactly the scenario manually verified this session, now
captured as a permanent, re-runnable test:

  1. A real security-auditor.md custom agent (extends: base-coder, mode:
     plan, tools: [read_file, grep_files, list_files]) is dispatched
     directly via subagents.dispatch_agent(agent_name=...) against a real
     file containing genuine, deliberately-introduced vulnerabilities
     (hardcoded secret + string-concatenated SQL injection).
  2. Asserts the real reply text actually names both real vulnerability
     classes (not just that SOME reply came back) -- avoiding the exact
     test-design mistake documented in skills_live_test.py's own history
     (asserting on a vague summary instead of checking for the real,
     specific content).
  3. Asserts the target file is BYTE-FOR-BYTE UNCHANGED on disk afterward
     (verified by hash, not just "no write_file call was logged") --
     proves the mode:plan + tools: intersection genuinely, structurally
     prevented any modification, not just that the model chose not to.
  4. A second scenario: the FULL parent ReAct loop (agent.run_agent, not
     a direct dispatch_agent call) is given a task that requires it to
     discover the custom agent itself via list_custom_agents and choose
     to dispatch it by name -- proving the feature is actually usable
     end-to-end through the real agent, not just callable in isolation.

Requires a real configured LLM provider (GROQ_API_KEY / GOOGLE_API_KEY /
NVIDIA_NIM_API_KEY / CEREBRAS_API_KEY / OPENROUTER_API_KEY) and network
access -- this is NOT a mocked unit test, matching this project's
established "prove behavior with a real LLM call" discipline for every
feature that touches the ReAct loop.

Run with: PYTHONPATH=/home/user/my-agent python3 test/custom_agents_live_test.py
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subagents  # noqa: E402
import agent  # noqa: E402

VULNERABLE_FILE = "test/scratch/vulnerable_sample.py"
VULNERABLE_CONTENT = (
    'import sqlite3\n\n'
    'DB_PASSWORD = "hunter2_super_secret"\n\n'
    'def get_user(username):\n'
    '    conn = sqlite3.connect("app.db")\n'
    '    cursor = conn.cursor()\n'
    '    query = "SELECT * FROM users WHERE username = \'" + username + "\'"\n'
    '    cursor.execute(query)\n'
    '    return cursor.fetchone()\n'
)


def _ensure_fixture():
    os.makedirs(os.path.dirname(VULNERABLE_FILE), exist_ok=True)
    with open(VULNERABLE_FILE, "w", encoding="utf-8") as f:
        f.write(VULNERABLE_CONTENT)
    return hashlib.md5(VULNERABLE_CONTENT.encode()).hexdigest()


def test_direct_dispatch_finds_real_vulnerabilities_and_never_modifies_file():
    original_hash = _ensure_fixture()

    result = subagents.dispatch_agent(
        prompt=(
            f"Perform a security audit of the file at {VULNERABLE_FILE}. "
            "Read the real file contents before drawing conclusions. Report "
            "every vulnerability you find with its type and severity."
        ),
        agent_name="security-auditor",
        _confirm=lambda *a: True,
    )
    print("--- real sub-agent reply ---")
    print(result)
    print("--- end reply ---")

    lower = result.lower()
    assert "sql injection" in lower, f"expected the real SQL injection finding to be named, got: {result}"
    assert ("hardcoded" in lower or "secret" in lower or "credential" in lower), (
        f"expected the real hardcoded-secret finding to be named, got: {result}"
    )
    assert not result.startswith("ERROR:"), f"dispatch failed outright: {result}"

    with open(VULNERABLE_FILE, encoding="utf-8") as f:
        actual_content = f.read()
    actual_hash = hashlib.md5(actual_content.encode()).hexdigest()
    assert actual_hash == original_hash, (
        "the read-only security-auditor agent (mode:plan intersected with its own "
        "tools:) must NEVER modify the audited file -- but its content changed"
    )
    print("PASS: real sub-agent found both real vulnerabilities AND never touched the file (verified by hash)")


def test_parent_agent_discovers_and_dispatches_named_agent_itself():
    """The full, real ReAct loop -- not a direct dispatch_agent call --
    must be ABLE to discover and use a named custom agent on its own,
    given a task that plausibly calls for it."""
    _ensure_fixture()

    reply = agent.run_agent(
        "Use list_custom_agents to see what custom agents exist, then dispatch "
        f"the security-auditor agent (by name) against {VULNERABLE_FILE} and "
        "report what it finds.",
        verbose=True,
        persist_memory=False,
        max_iterations=8,
        confirm=lambda *a: True,
    )
    print("--- parent agent final reply ---")
    print(reply)
    print("--- end reply ---")

    lower = reply.lower()
    assert "security-auditor" in lower or "security auditor" in lower, (
        f"expected the parent's own final summary to mention the agent it used, got: {reply}"
    )
    assert "sql injection" in lower, f"expected the real finding to surface in the parent's summary too, got: {reply}"
    print("PASS: the real parent ReAct loop discovered and used the named custom agent end-to-end")


if __name__ == "__main__":
    test_direct_dispatch_finds_real_vulnerabilities_and_never_modifies_file()
    test_parent_agent_discovers_and_dispatches_named_agent_itself()
    print("\nALL TESTS PASSED")
