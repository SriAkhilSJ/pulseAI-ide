#!/usr/bin/env python3
"""
guard_force_push.py -- a real PreToolUse hook command for the git-safety
example plugin.

Contract (see plugins.py's run_hooks docstring for the full, real spec
this project implements): reads ONE JSON object from stdin
({"tool_name": ..., "tool_args": {...}}), and may print a JSON object to
stdout to make a decision. Printing nothing (or invalid JSON) means "no
opinion, allow" -- this hook only ever prints something when it actually
wants to block.

This hook blocks `git push --force` (and `-f`) specifically against
`main`/`master` -- a real, useful guardrail (force-pushing a protected
branch can destroy other people's work), demonstrating a genuine
PreToolUse use case rather than a toy example.
"""
import json
import re
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input -- fail open, print nothing

    if payload.get("tool_name") != "run_command":
        return

    cmd = payload.get("tool_args", {}).get("cmd", "")
    is_force_push = re.search(r"\bgit\s+push\b.*\B(--force|-f)\b", cmd) is not None
    targets_protected_branch = re.search(r"\b(main|master)\b", cmd) is not None

    if is_force_push and targets_protected_branch:
        print(json.dumps({
            "decision": "block",
            "reason": (
                "git-safety plugin: refusing to force-push to a protected branch "
                "(main/master). If you really need to do this, run it manually "
                "outside the agent."
            ),
        }))
    # else: print nothing -- allow.


if __name__ == "__main__":
    main()
