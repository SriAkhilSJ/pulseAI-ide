"""
Direct, live test of process_manager.py (Gap 2 fix).

Confirms (all against REAL subprocesses, not mocks):
  1. start() returns a real, live PID immediately.
  2. list_processes() reports it while alive.
  3. stop() actually kills it (verified via os.kill probe AND ps aux).
  4. cleanup_all() kills multiple tracked processes at once.
  5. cleanup_orphans_from_previous_run() kills a process left in the
     registry from a "previous" run (simulated by writing the state file
     directly, bypassing start()).

Run with: PYTHONPATH=/home/user/my-agent python3 test/process_manager_test.py
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import process_manager as pm  # noqa: E402


def _is_running(pid: int) -> bool:
    # Reap zombies first (we're the direct parent in every test here) --
    # see process_manager._is_alive's docstring for why a plain os.kill(pid,0)
    # check alone is NOT sufficient (it reports zombies as "alive").
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        pass
    except ProcessLookupError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_start_and_track():
    result = pm.start("sleep 30", name="test-sleep-30")
    handle, pid = result["handle"], result["pid"]
    time.sleep(0.3)
    assert _is_running(pid), "process should be alive right after start()"
    procs = pm.list_processes()
    assert handle in procs, f"handle {handle} should appear in list_processes()"
    assert procs[handle]["pid"] == pid
    print(f"PASS: start() launched real pid={pid}, tracked under handle={handle}")
    return handle, pid


def test_stop_kills_it(handle, pid):
    msg = pm.stop(handle)
    print("stop() said:", msg)
    time.sleep(0.3)
    assert not _is_running(pid), f"pid {pid} should be dead after stop()"
    procs = pm.list_processes()
    assert handle not in procs, "handle should be removed from registry after stop()"
    # Cross-check with a totally independent tool (ps), not just os.kill.
    ps_out = subprocess.run(["ps", "-p", str(pid)], capture_output=True, text=True).stdout
    assert str(pid) not in ps_out, f"ps still shows pid {pid} running: {ps_out}"
    print("PASS: stop() killed the real process, confirmed independently via `ps`")


def test_cleanup_all_multiple():
    r1 = pm.start("sleep 30", name="multi-1")
    r2 = pm.start("sleep 30", name="multi-2")
    time.sleep(0.3)
    assert _is_running(r1["pid"]) and _is_running(r2["pid"])
    msg = pm.cleanup_all()
    print("cleanup_all() said:\n" + msg)
    time.sleep(0.3)
    assert not _is_running(r1["pid"])
    assert not _is_running(r2["pid"])
    assert pm.list_processes() == {}
    print("PASS: cleanup_all() killed both tracked processes")


def test_orphan_cleanup_from_previous_run():
    # Simulate a PREVIOUS agent process that started something and then
    # crashed without calling cleanup_all() -- write the registry directly,
    # bypassing start(), to prove cleanup_orphans reads real on-disk state.
    proc = subprocess.Popen("sleep 30", shell=True, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    assert _is_running(proc.pid)
    pm.STATE_FILE.write_text(json.dumps({
        "orphan1": {"pid": proc.pid, "cmd": "sleep 30", "cwd": ".", "name": "orphan-from-crash", "start_time": time.time()}
    }))
    msg = pm.cleanup_orphans_from_previous_run()
    print("cleanup_orphans_from_previous_run() said:", msg)
    time.sleep(0.3)
    assert not _is_running(proc.pid), "orphaned process should have been killed"
    assert pm.list_processes() == {}
    print("PASS: orphan cleanup killed a process left over from a simulated crashed run")


if __name__ == "__main__":
    handle, pid = test_start_and_track()
    test_stop_kills_it(handle, pid)
    test_cleanup_all_multiple()
    test_orphan_cleanup_from_previous_run()
    print("\nALL TESTS PASSED")
