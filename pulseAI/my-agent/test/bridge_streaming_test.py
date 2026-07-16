"""
Live, real-subprocess test of end-to-end command streaming through
bridge_server.py -- the exact validation scenario originally requested:
run a `for i in {1..N}; do echo step $i; sleep 1; done` loop and confirm
"step 1", "step 2", ... arrive one by one over real wall-clock time via
real "command_output" events, not all at once after the loop finishes.

Run with: PYTHONPATH=/home/user/my-agent python3 test/bridge_streaming_test.py
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
            time.sleep(0.05)
        return None

    def count(self, predicate):
        with self.lock:
            return sum(1 for m in self.messages if predicate(m))

    def snapshot(self, predicate):
        with self.lock:
            return [m for m in self.messages if predicate(m)]

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


def test_command_output_events_stream_incrementally():
    client = BridgeClient()
    try:
        ready = client.wait_for(lambda m: m.get("type") == "ready", timeout=10)
        assert ready is not None

        client.send({
            "type": "run", "id": "streamtest1",
            "input": (
                "Call run_command directly with cmd='for i in 1 2 3 4 5; do echo step $i; "
                "sleep 0.5; done' and timeout=15. Just call the tool, don't ask me first."
            ),
        })

        # Poll for command_output events arriving OVER TIME, recording when
        # each one shows up (real wall-clock timestamps), not just whether
        # they eventually all exist.
        seen_at = {}
        start = time.time()
        deadline = start + 60
        while time.time() < deadline and len(seen_at) < 5:
            outputs = client.snapshot(lambda m: m.get("type") == "command_output" and m.get("id") == "streamtest1")
            for o in outputs:
                if o["line"] not in seen_at:
                    seen_at[o["line"]] = time.time() - start
            if len(seen_at) >= 5:
                break
            time.sleep(0.1)

        print("lines seen at (relative seconds):", seen_at)
        assert len(seen_at) == 5, f"expected 5 command_output events, got {len(seen_at)}: {seen_at}"
        for i in range(1, 6):
            assert f"step {i}" in seen_at, f"missing step {i}"

        ordered_times = [seen_at[f"step {i}"] for i in range(1, 6)]
        assert ordered_times == sorted(ordered_times), "lines should arrive in order"
        spread = ordered_times[-1] - ordered_times[0]
        assert spread > 1.5, (
            f"all command_output events arrived within {spread:.2f}s of each other -- "
            f"this looks like batched delivery at the end, not real per-line streaming"
        )
        print(f"PASS: command_output events genuinely streamed one-by-one over {spread:.2f}s "
              f"(the exact scenario originally requested: 'step 1'...'step 5' appearing incrementally, not all at once)")

        result = client.wait_for(lambda m: m.get("type") == "result" and m.get("id") == "streamtest1", timeout=30)
        assert result is not None
        print("PASS: final result message still arrives correctly after streaming completes")
    finally:
        client.close()


if __name__ == "__main__":
    test_command_output_events_stream_incrementally()
    print("\nALL TESTS PASSED")
