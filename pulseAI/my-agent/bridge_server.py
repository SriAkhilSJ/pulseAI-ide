"""
bridge_server.py
-----------------
The real process the VS Code extension spawns as a child process and talks
to over stdin/stdout, one JSON object per line (newline-delimited JSON,
NOT ACP's JSON-RPC 2.0 framing -- that protocol was never fact-checked in
this project, so this bridge is deliberately built ONLY on infrastructure
that has already been written and tested: agent.run_agent(),
confirm_bridge.webview_confirm(), and a2ui.py's manifest files. If/when
the real ACP wire spec gets verified, this can be reframed as an ACP
server without changing the underlying agent-calling logic).

Wire format (newline-delimited JSON, one object per line each direction):

  Extension -> bridge (stdin):
    {"type": "run", "id": "<request id>", "input": "<user message>", "mission_id": "<optional>"}
    {"type": "confirm_response", "request_id": "<id from a pending confirm push>", "approved": true}

  Bridge -> extension (stdout):
    {"type": "ready"}
    {"type": "log", "id": "<request id>", "event": "Thought"|"Action"|"Observation", "payload": "..."}
    {"type": "confirm_request", "request_id": "<id>", "tool": "...", "args": {...}, "reason": "...", "diff": "..."}
    {"type": "command_output", "id": "<request id>", "line": "<one line of a running run_command's output>"}
    {"type": "result", "id": "<request id>", "reply": "<final agent reply>"}
    {"type": "error", "id": "<request id>|null", "message": "..."}

`command_output` (new): pushed once per line, AS a long-running run_command
call actually produces output, via agent.run_agent()'s on_command_line
parameter -- lets the webview show live progress for something like `npm
install` or `pytest` instead of silence for the whole duration. Reuses
tools.run_command()'s existing streaming support (see tools.py's
run_command docstring) end-to-end through the real dispatch chain; nothing
about the LLM's own run_command tool call changes -- on_command_line isn't
a tool-callable parameter, it's purely how THIS bridge process observes
output as it happens.

Every real tool call the agent makes still goes through the SAME
TOOL_FUNCTIONS/confirm() machinery already tested elsewhere in this
project (agent.py, tools.py, confirm_bridge.py) -- this file only adds the
stdin/stdout framing and a background thread pump so confirm_bridge's
polling-based pending-request mechanism can be pushed to the extension
instead of just sitting in .agent_ui/confirm_requests/ waiting to be
polled by something else.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback

import agent
import confirm_bridge


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _log_for(request_id: str):
    def _log(event: str, payload: str) -> None:
        _emit({"type": "log", "id": request_id, "event": event, "payload": payload})
    return _log


def _on_command_line_for(request_id: str):
    """Returns a callback matching agent.run_agent()'s on_command_line
    signature (line: str) -> None, pushing each line as its own
    command_output event tagged with this request's id -- so the
    extension can tell which in-flight run() a given streamed line
    belongs to when multiple requests are active concurrently."""
    def _on_line(line: str) -> None:
        _emit({"type": "command_output", "id": request_id, "line": line})
    return _on_line


def _confirm_via_extension(name: str, args: dict, reason: str, diff: str | None = None) -> bool:
    """Passed as agent.py's `confirm=` callback. Delegates the actual
    blocking wait to confirm_bridge.webview_confirm (already tested:
    see test/confirm_bridge_test.py / test/a2ui_live_test.py) but ALSO
    pushes a confirm_request event over stdout immediately, so the
    extension doesn't have to separately poll .agent_ui/confirm_requests/
    -- it gets pushed the request the moment it's created, then answers it
    by writing a response, same as any other responder confirm_bridge
    already supports (extension-side and test-side responders are
    interchangeable, since confirm_bridge doesn't know or care who answers
    a request -- it just watches for the response file)."""
    # Push a notification, then let confirm_bridge do the actual (already
    # tested) blocking wait/timeout/fail-closed logic -- no duplicated
    # blocking logic here.
    pushed = {"tool": name, "args": {k: v for k, v in args.items() if k != "content"}, "reason": reason, "diff": diff}

    # We need the request_id confirm_bridge assigns internally, but it
    # doesn't expose one until AFTER webview_confirm writes the request
    # file. Poll briefly for the newest pending request matching this
    # tool/reason right after triggering webview_confirm in a thread, so
    # we can push its real id to the extension.
    result_holder: dict = {}

    def _wait():
        result_holder["approved"] = confirm_bridge.webview_confirm(name, args, reason, diff)

    t = threading.Thread(target=_wait, daemon=True)
    t.start()

    request_id = None
    deadline = time.time() + 2.0
    while time.time() < deadline and request_id is None:
        for req in confirm_bridge.list_pending_requests():
            if req["tool"] == name and req["reason"] == reason:
                request_id = req["id"]
                break
        if request_id is None:
            time.sleep(0.05)

    if request_id:
        _emit({"type": "confirm_request", "request_id": request_id, **pushed})
    else:
        # Extremely unlikely (webview_confirm should write the file
        # almost instantly) -- if it happens, the extension just won't
        # get a push notice, but confirm_bridge's own timeout/fail-closed
        # behavior still protects the actual gate.
        _emit({"type": "log", "id": None, "event": "Warning",
               "payload": "Could not locate the pending confirm request id to push to the extension in time."})

    t.join()
    return bool(result_holder.get("approved", False))


def _handle_run(msg: dict) -> None:
    request_id = msg.get("id", "")
    user_input = msg.get("input", "")
    mission_id = msg.get("mission_id")
    try:
        if mission_id:
            reply = agent.run_mission(
                user_input, mission_id=mission_id, verbose=True,
                log=_log_for(request_id), confirm=_confirm_via_extension,
                on_command_line=_on_command_line_for(request_id),
            )
        else:
            reply = agent.run_agent(
                user_input, verbose=True,
                log=_log_for(request_id), confirm=_confirm_via_extension,
                on_command_line=_on_command_line_for(request_id),
            )
        _emit({"type": "result", "id": request_id, "reply": reply})
    except Exception as e:
        _emit({"type": "error", "id": request_id, "message": f"{type(e).__name__}: {e}"})
        traceback.print_exc(file=sys.stderr)


def _handle_confirm_response(msg: dict) -> None:
    request_id = msg.get("request_id", "")
    approved = bool(msg.get("approved", False))
    confirm_bridge.respond(request_id, approved)


def main() -> None:
    _emit({"type": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _emit({"type": "error", "id": None, "message": f"invalid JSON from extension: {e}"})
            continue

        msg_type = msg.get("type")
        if msg_type == "run":
            # Run each request in its own thread so a long-running agent
            # call doesn't block reading the NEXT stdin line (e.g. a
            # confirm_response for a DIFFERENT in-flight request, or the
            # user starting a second query before the first finishes).
            threading.Thread(target=_handle_run, args=(msg,), daemon=True).start()
        elif msg_type == "confirm_response":
            _handle_confirm_response(msg)
        else:
            _emit({"type": "error", "id": msg.get("id"), "message": f"unknown message type: {msg_type!r}"})


if __name__ == "__main__":
    main()
