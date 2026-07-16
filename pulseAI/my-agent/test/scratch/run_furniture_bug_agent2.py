import os
import sys
import time
sys.path.insert(0, '/home/user/my-agent')
os.chdir('/home/user/my-agent')

import llm_client
# Measurement-only override: `timeout_seconds` is bound to
# DEFAULT_CHAT_TIMEOUT_SECONDS at chat_completion()'s OWN def-time (import
# time) -- confirmed live that reassigning the module constant afterward
# has ZERO effect on already-bound default args, a real Python gotcha, not
# an agent bug. agent.py's call site never passes timeout_seconds
# explicitly, so the wrapper below injects it directly on every call
# instead. Free-tier providers have shown high latency variance (0.9s-84s
# per call observed directly against the real Router) -- bumping the
# wall-clock budget for THIS stress-test run only (not touching the
# shipped default of 90s in llm_client.py).
_orig_chat_completion = llm_client.chat_completion
call_count = {"n": 0}

def _counting_chat_completion(*args, **kwargs):
    call_count["n"] += 1
    print(f"\n=== LLM CALL #{call_count['n']} ===", flush=True)
    t0 = time.time()
    kwargs.setdefault("timeout_seconds", 180)
    try:
        r = _orig_chat_completion(*args, **kwargs)
        print(f"    (took {time.time()-t0:.1f}s)", flush=True)
        return r
    except Exception as e:
        print(f"    (FAILED after {time.time()-t0:.1f}s: {type(e).__name__}: {e})", flush=True)
        raise

llm_client.chat_completion = _counting_chat_completion

import permissions
from permissions import PermissionMode

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
