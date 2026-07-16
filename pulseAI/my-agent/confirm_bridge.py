"""
confirm_bridge.py
------------------
The REAL, blocking bridge between agent.py's confirmation gate and a
webview/extension UI -- unlike a2ui.py's advisory hints (which the agent
can call as an ordinary tool and which have no power to block anything),
this module provides a `confirm(name, args, reason, diff) -> bool`
function with the EXACT signature agent.py's run_agent()/run_mission()
already expect for their `confirm` parameter (see agent.py's
_default_confirm and _dispatch_tool_call) -- so it's a drop-in replacement
for the terminal y/N prompt, not a new, separate mechanism the model has
to voluntarily cooperate with.

How it actually blocks the RIGHT thing: this function is called by
agent.py's own Python code, synchronously, in the same process as the
ReAct loop -- BEFORE the flagged tool call runs. It writes a pending
request file, then polls (with a timeout) for a response file that only
gets written once whatever is watching this directory (an extension host,
a test harness, a human) explicitly approves or denies. Until that
response file appears, run_agent() is genuinely paused: the destructive
run_command/write_file call has NOT executed yet. This is what makes it a
real gate rather than a suggestion -- contrast with a2ui.py's render_ui,
which is just informational display the model could call and then ignore.

This is intentionally file-based polling FROM PYTHON, not from inside a
webview -- that distinction matters and is not a contradiction of the
"webviews can't fetch() a local path" limitation documented in a2ui.py.
This code runs in the same OS process as the agent (full filesystem
access, no sandboxing), so polling a file here is completely normal and
safe; it's specifically fetch()-from-inside-the-sandboxed-webview-iframe
that doesn't work, which is a different process entirely (VS Code's
renderer, not this Python process).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

REQUEST_DIR = Path(".agent_ui") / "confirm_requests"
DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes -- long enough for a human to notice and respond,
                                # short enough that a truly unattended session doesn't hang forever.


def _ensure_dirs() -> None:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)


def webview_confirm(
    name: str,
    args: dict,
    reason: str,
    diff: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """
    Drop-in replacement for agent.py's _default_confirm, matching its
    exact (name, args, reason, diff) -> bool signature. Pass this as the
    `confirm=` argument to run_agent()/run_mission() to route confirmation
    prompts through a webview instead of a terminal y/N.

    Fails CLOSED (returns False / denies the action) on timeout or any
    error reading the response -- same principle as agent.py's own
    _default_confirm treating EOF/KeyboardInterrupt as "n": an ambiguous
    or missing answer must never be treated as approval for a destructive
    action.
    """
    _ensure_dirs()
    request_id = uuid.uuid4().hex[:12]
    request_path = REQUEST_DIR / f"{request_id}.request.json"
    response_path = REQUEST_DIR / f"{request_id}.response.json"

    request = {
        "id": request_id,
        "tool": name,
        # Never include raw file content in the request -- diff is already
        # a diff (bounded, human-scannable), but a full "content" argument
        # (e.g. write_file's) could be arbitrarily large and isn't needed
        # to make an approve/deny decision; the diff conveys what changes.
        "args": {k: v for k, v in args.items() if k != "content"},
        "reason": reason,
        "diff": diff,
        "created_at": time.time(),
    }
    request_path.write_text(json.dumps(request, indent=2))

    deadline = time.time() + timeout_seconds
    try:
        while time.time() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text())
                    approved = bool(response.get("approved", False))
                except (json.JSONDecodeError, OSError):
                    approved = False  # malformed response -- fail closed, not open
                _cleanup(request_path, response_path)
                return approved
            time.sleep(0.3)
    finally:
        # Always clean up the request file even on timeout, so a stale
        # pending request doesn't confuse a later, unrelated call that
        # happens to reuse... (it can't, request_id is unique, but leaving
        # stale files around is still just clutter worth avoiding).
        pass

    # Timed out waiting for a response -- fail closed.
    _cleanup(request_path, response_path)
    return False


def _cleanup(request_path: Path, response_path: Path) -> None:
    for p in (request_path, response_path):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def list_pending_requests() -> list[dict]:
    """For an extension host (or a test) to discover requests currently
    awaiting a response."""
    _ensure_dirs()
    pending = []
    for p in sorted(REQUEST_DIR.glob("*.request.json")):
        request_id = p.stem.replace(".request", "")
        if (REQUEST_DIR / f"{request_id}.response.json").exists():
            continue  # already answered, about to be cleaned up by the waiter
        try:
            pending.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return pending


def respond(request_id: str, approved: bool) -> None:
    """For an extension host (or a test) to answer a pending request."""
    _ensure_dirs()
    response_path = REQUEST_DIR / f"{request_id}.response.json"
    response_path.write_text(json.dumps({"approved": approved, "responded_at": time.time()}))
