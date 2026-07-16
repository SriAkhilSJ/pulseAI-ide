import sys
sys.path.insert(0, "/home/user/my-agent")
import agent

reply = agent.run_agent(
    "Use list_custom_agents to see what custom agents exist, then dispatch the "
    "security-auditor agent (by name) against test/scratch/vulnerable_sample.py "
    "and report what it finds.",
    verbose=True,
    persist_memory=False,
    max_iterations=8,
    confirm=lambda *a: True,
)
print("=" * 60)
print("FINAL REPLY:")
print(reply)
