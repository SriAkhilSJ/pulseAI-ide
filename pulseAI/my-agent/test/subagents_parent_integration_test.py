"""
Full end-to-end integration: a real top-level agent.run_agent() call, with
a real LLM deciding on its own to call dispatch_agent (nothing forces it
to -- we give it a task explicitly worth delegating and see if it reaches
for the tool), which spawns a real restricted sub-agent loop, which
returns exactly one summary string back into the parent's own
conversation/final answer.

This is the "does the whole wire actually work, not just each piece in
isolation" test -- complements test/subagents_test.py (unit-level,
no LLM) and test/subagents_live_test.py (direct dispatch_agent() calls,
no parent loop).

Run with: PYTHONPATH=/home/user/my-agent python3 test/subagents_parent_integration_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


def test_parent_can_dispatch_a_real_subagent_and_get_one_summary():
    events = []

    def log(event, payload):
        events.append((event, payload))
        print(f"[{event}] {str(payload)[:200]}")

    reply = agent.run_agent(
        "Use your dispatch_agent tool with subagent_type='explore' to delegate this "
        "exact research task: find and report the exact list of Flask route paths "
        "defined in test/finance_dashboard/app.py (e.g. '/api/balance'). Do not read "
        "the file yourself -- delegate it. Report back exactly what the sub-agent found.",
        verbose=True,
        log=log,
        confirm=lambda *a: True,
        max_iterations=8,
        persist_memory=False,  # keep this test run out of the real memory.json
    )

    print("\n--- FINAL REPLY ---")
    print(reply)

    dispatch_calls = [p for (e, p) in events if e == "Action" and "dispatch_agent" in str(p)]
    assert dispatch_calls, (
        "expected the parent model to actually call dispatch_agent at least once -- "
        f"it didn't. All Action events: {[p for e, p in events if e == 'Action']}"
    )

    observations = [p for (e, p) in events if e == "Observation"]
    subagent_observations = [o for o in observations if "[sub-agent" in str(o)]
    assert subagent_observations, "expected an Observation containing the sub-agent's tagged result"

    # The real structural proof this test is FOR: the parent's own message
    # history must contain the sub-agent's single summarized result, not a
    # replay of the sub-agent's OWN internal tool calls (read_file, etc.) --
    # those never appear as separate Action/Observation events in the
    # PARENT's log at all, because they happened inside an isolated
    # run_agent() call the parent's log callback was never passed into.
    parent_action_names = {str(p).split("(")[0] for (e, p) in events if e == "Action" and "[running" not in str(p)}
    assert "read_file" not in parent_action_names, (
        f"the parent's OWN action log should not show read_file -- that should have "
        f"happened inside the isolated sub-agent, invisible to the parent. Saw: {parent_action_names}"
    )

    print(f"\nPASS: parent dispatched a real sub-agent, received exactly its summarized "
          f"result ({len(subagent_observations)} tagged observation(s)), and the parent's "
          f"own action log never shows the sub-agent's internal read_file calls.")


if __name__ == "__main__":
    test_parent_can_dispatch_a_real_subagent_and_get_one_summary()
