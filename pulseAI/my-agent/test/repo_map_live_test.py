"""
LIVE end-to-end test of repo_map_query against a REAL LLM (not mocked):
given a task about permission modes / confirmation gating, does a real
model choose to call repo_map_query, and does the returned ranking
actually surface the right, real files (permissions.py, agent.py) for
that concept -- verified against this project's own real, live codebase,
not a synthetic fixture.

Run with: PYTHONPATH=/home/user/my-agent python3 test/repo_map_live_test.py
(requires at least one real provider API key in .env)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import repo_map  # noqa: E402


def test_repo_map_query_ranks_relevant_files_for_a_real_concept():
    """Direct, non-LLM sanity check first: does querying for a real
    concept in THIS project surface the actually-relevant real files."""
    result = repo_map.get_repo_map(query="permission mode confirmation gate", max_tokens=2000)
    assert result, "expected a non-empty repo map"
    print(result[:600])
    file_lines = [l for l in result.splitlines() if l and not l.startswith(" ")]
    top_10 = file_lines[:10]
    assert "permissions.py" in top_10, f"expected permissions.py in the top 10 for a permission-mode query, got: {top_10}"
    print(f"\nPASS: querying for 'permission mode confirmation gate' surfaces the real permissions.py in the top 10: {top_10}")


def test_real_llm_uses_repo_map_query_to_orient_in_the_codebase():
    """The full live test: does a real model, given an unfamiliar-codebase
    style task, choose to call repo_map_query before diving into
    list_files/read_file one at a time?"""
    events = []

    def log(event, payload):
        events.append((event, payload))
        print(f"[{event}] {str(payload)[:200]}")

    reply = agent.run_agent(
        "I'm new to this codebase. Before making any changes, get an overview of which files "
        "are most structurally important for how permission modes and the confirmation gate work, "
        "then tell me the 2-3 most relevant files and why, based on what you find. Do not read full "
        "file contents -- just use the overview tool.",
        verbose=True, log=log, confirm=lambda *a: True,
        persist_memory=False, max_iterations=6,
    )
    print("\n--- FINAL REPLY ---")
    print(reply)

    repo_map_calls = [p for (e, p) in events if e == "Action" and "repo_map_query" in str(p)]
    assert repo_map_calls, (
        f"expected the model to call repo_map_query for an 'overview of the codebase' task -- it "
        f"didn't. All Action events: {[p for e, p in events if e == 'Action']}"
    )
    print(f"\nPASS: a real model chose to call repo_map_query on its own for an overview-style task: {repo_map_calls[0][:100]}")

    mentions_relevant_file = "permissions.py" in reply or "agent.py" in reply
    assert mentions_relevant_file, f"expected the final reply to mention permissions.py or agent.py, got:\n{reply}"
    print("PASS: the model's final answer correctly names a real, relevant file from the repo map's real output")


if __name__ == "__main__":
    test_repo_map_query_ranks_relevant_files_for_a_real_concept()
    print("=" * 70)
    test_real_llm_uses_repo_map_query_to_orient_in_the_codebase()
    print("\nALL LIVE TESTS PASSED")
