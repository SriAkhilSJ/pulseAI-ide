import os
import sys
import time
sys.path.insert(0, '/home/user/my-agent')
os.chdir('/home/user/my-agent')

import llm_client
import permissions
from permissions import PermissionMode

# Count real LLM calls (== ReAct loop "turns"/iterations) without touching
# agent.py's internals -- wrap the actual chat_completion entry point the
# ReAct loop calls once per iteration.
_orig_chat_completion = llm_client.chat_completion
call_count = {"n": 0}

def _counting_chat_completion(*args, **kwargs):
    call_count["n"] += 1
    print(f"\n=== LLM CALL #{call_count['n']} ===", flush=True)
    return _orig_chat_completion(*args, **kwargs)

llm_client.chat_completion = _counting_chat_completion

task = open('test/scratch/furniture_bug_task.txt', encoding='utf-8').read()

start = time.time()
reply = permissions.run_agent_with_mode(
    task,
    mode=PermissionMode.ACCEPT_EDITS,
    base_confirm=lambda *a: True,
    verbose=True,
    persist_memory=False,
    max_iterations=25,
)
elapsed = time.time() - start

print("=" * 70)
print(f"TOTAL LLM CALLS (turns): {call_count['n']}")
print(f"WALL CLOCK: {elapsed:.1f}s")
print("=" * 70)
print("FINAL REPLY:")
print(reply)
