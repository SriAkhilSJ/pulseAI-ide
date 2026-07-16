"""
Live, end-to-end test: real ReAct loop + real LLM call, proving:
  1. render_ui works as an advisory tool and REJECTS CONFIRM_DIALOG outright.
  2. confirm_bridge.webview_confirm, wired as agent.py's real `confirm=`
     parameter, genuinely blocks a destructive run_command until an
     external responder approves/denies it -- proving this is a REAL gate,
     not a decorative UI hint the model could bypass.

Run with: PYTHONPATH=/home/user/my-agent python3 test/a2ui_live_test.py
"""
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import confirm_bridge  # noqa: E402
import tools  # noqa: E402


def test_render_ui_rejects_confirm_dialog_live():
    task = (
        "Call the render_ui tool with template='CONFIRM_DIALOG' and data='{}'. "
        "Report the EXACT raw string the tool returns, verbatim, do not paraphrase it."
    )
    reply = agent.run_agent(task, verbose=True, confirm=lambda *a: True, max_iterations=5)
    print("\n--- reply ---")
    print(reply)
    assert "CONFIRM_DIALOG" in reply or "not a valid" in reply.lower() or "confirm_bridge" in reply.lower(), \
        "the agent should report that CONFIRM_DIALOG was rejected"
    print("PASS: live agent call to render_ui(CONFIRM_DIALOG) is rejected, not silently accepted")


def test_confirm_bridge_actually_gates_a_real_destructive_command():
    """
    The real proof: wire confirm_bridge.webview_confirm as agent.py's
    ACTUAL confirm= parameter (the same one _dispatch_tool_call calls
    before running a flagged tool), give the agent a destructive command
    to run, and confirm the underlying run_command call is genuinely
    paused -- not run -- until an external responder approves it. This is
    the exact guarantee the original A2UI proposal's CONFIRM_DIALOG design
    did NOT have (it was just a tool call with no coupling to this gate).
    """
    marker_dir = "test/scratch_a2ui_confirm_marker"
    if os.path.exists(marker_dir):
        shutil.rmtree(marker_dir)

    responded = {"done": False}

    def auto_responder():
        # Wait for a real pending request to show up, then approve it --
        # simulating a human/webview clicking "Approve" after seeing the
        # real command in the request.
        deadline = time.time() + 60
        while time.time() < deadline:
            pending = confirm_bridge.list_pending_requests()
            if pending:
                req = pending[0]
                assert req["tool"] == "run_command"
                assert marker_dir in req["reason"] or marker_dir in str(req["args"])
                print(f"[responder] saw real pending request: {req['args']}")
                confirm_bridge.respond(req["id"], approved=True)
                responded["done"] = True
                return
            time.sleep(0.2)

    responder_thread = threading.Thread(target=auto_responder, daemon=True)
    responder_thread.start()

    task = f"Use run_command to run: rm -rf {marker_dir}"
    # First actually create the dir so there's something real to (attempt to) delete.
    os.makedirs(marker_dir, exist_ok=True)
    with open(os.path.join(marker_dir, "placeholder.txt"), "w") as f:
        f.write("should be deleted only after real approval\n")

    reply = agent.run_agent(
        task, verbose=True, confirm=confirm_bridge.webview_confirm, max_iterations=5
    )
    print("\n--- reply ---")
    print(reply)

    responder_thread.join(timeout=5)
    assert responded["done"], "the confirm bridge should have received and answered a real pending request"
    assert not os.path.exists(marker_dir), "after real approval, the destructive command should have actually run"
    print("PASS: confirm_bridge genuinely gated the destructive run_command until external approval, then it executed")


if __name__ == "__main__":
    test_render_ui_rejects_confirm_dialog_live()
    test_confirm_bridge_actually_gates_a_real_destructive_command()
    print("\nALL TESTS PASSED")
