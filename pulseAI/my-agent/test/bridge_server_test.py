"""
Direct, live test of bridge_server.py, run as a REAL subprocess
communicating over real stdin/stdout pipes -- exactly how the VS Code
extension will actually talk to it (not an in-process import test).

Run with: PYTHONPATH=/home/user/my-agent python3 test/bridge_server_test.py
"""
import json
import os
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BridgeClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(REPO_ROOT, "bridge_server.py")],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.messages = []
        self.lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                self.messages.append(msg)

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, predicate, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for m in self.messages:
                    if predicate(m):
                        return m
            time.sleep(0.1)
        return None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def test_ready_message_on_startup():
    client = BridgeClient()
    try:
        ready = client.wait_for(lambda m: m.get("type") == "ready", timeout=10)
        assert ready is not None, "bridge should emit a ready message on startup"
        print("PASS: bridge_server emits {'type': 'ready'} on startup")
    finally:
        client.close()


def test_run_request_produces_log_and_result():
    client = BridgeClient()
    try:
        ready = client.wait_for(lambda m: m.get("type") == "ready", timeout=10)
        assert ready

        client.send({"type": "run", "id": "req1", "input": "Say the word 'pong' and nothing else."})

        result = client.wait_for(lambda m: m.get("type") == "result" and m.get("id") == "req1", timeout=90)
        assert result is not None, "expected a result message for req1"
        print("result reply:", result["reply"])
        assert "pong" in result["reply"].lower()

        logs = [m for m in client.messages if m.get("type") == "log" and m.get("id") == "req1"]
        print(f"PASS: got {len(logs)} log event(s) and a final result for a real run request")
    finally:
        client.close()


def test_confirm_request_is_pushed_and_can_be_answered():
    import shutil
    marker_dir = os.path.join(REPO_ROOT, "test", "scratch_bridge_confirm_marker")
    if os.path.exists(marker_dir):
        shutil.rmtree(marker_dir)
    os.makedirs(marker_dir)
    with open(os.path.join(marker_dir, "f.txt"), "w") as f:
        f.write("delete me only after approval\n")

    client = BridgeClient()
    try:
        ready = client.wait_for(lambda m: m.get("type") == "ready", timeout=10)
        assert ready

        client.send({
            "type": "run", "id": "req2",
            "input": (
                "Call the run_command tool directly with cmd='rm -rf "
                "test/scratch_bridge_confirm_marker'. Do NOT ask me in your reply first -- "
                "just call the tool now; the system itself will pause and ask for real "
                "confirmation automatically before the command actually executes, so there "
                "is nothing for you to ask about separately."
            ),
        })

        confirm_req = client.wait_for(lambda m: m.get("type") == "confirm_request", timeout=90)
        assert confirm_req is not None, "expected a confirm_request to be pushed over stdout"
        print("confirm_request pushed:", confirm_req)
        assert confirm_req["tool"] == "run_command"
        assert "scratch_bridge_confirm_marker" in json.dumps(confirm_req["args"])

        # Directory must NOT be deleted yet -- still waiting on our response.
        assert os.path.exists(marker_dir), "must not run the destructive command before approval"
        print("PASS: destructive command genuinely paused, waiting on confirm response")

        client.send({"type": "confirm_response", "request_id": confirm_req["request_id"], "approved": True})

        result = client.wait_for(lambda m: m.get("type") == "result" and m.get("id") == "req2", timeout=30)
        assert result is not None
        print("PASS: sending confirm_response unblocked the run and produced a result")

        time.sleep(0.5)
        assert not os.path.exists(marker_dir), "after approval, the destructive command should have actually run"
        print("PASS: directory was actually deleted only after the pushed confirm_request was approved")
    finally:
        client.close()
        if os.path.exists(marker_dir):
            shutil.rmtree(marker_dir)


if __name__ == "__main__":
    test_ready_message_on_startup()
    test_run_request_produces_log_and_result()
    test_confirm_request_is_pushed_and_can_be_answered()
    print("\nALL TESTS PASSED")
