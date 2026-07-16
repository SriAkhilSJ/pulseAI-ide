import os
import sys
sys.path.insert(0, '/home/user/my-agent')
os.chdir('/home/user/my-agent')

import permissions
from permissions import PermissionMode

task = open('test/scratch/furniture_fix_task.txt', encoding='utf-8').read()

reply = permissions.run_agent_with_mode(
    task,
    mode=PermissionMode.ACCEPT_EDITS,
    base_confirm=lambda *a: True,
    verbose=True,
    persist_memory=False,
    max_iterations=15,
)
print("=" * 70)
print("FINAL REPLY:")
print(reply)
