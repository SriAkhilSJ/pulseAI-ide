"""
process_manager.py
-------------------
Tracks background/long-running processes the agent starts on purpose (dev
servers like `flask run`, `python -m http.server`, `npm start`, etc.) so
they can be listed and cleanly killed instead of leaking past the task or
mission that started them.

Real bug this fixes: during the 3-mission finance-dashboard stress test, a
Flask dev server started via run_command's shell `&` backgrounding survived
past the mission/process that launched it at least twice, and required a
human/manual `pkill` to clean up -- confirmed independently in this session
too (see test/process_manager_test.py) that plain shell backgrounding gives
the agent no PID to track at all, so there was never anything to clean up
even if we wanted to.

Design notes / what this deliberately does NOT do:
  - It is a SEPARATE tool (`start_background_process` / `stop_background_process`
    / `list_background_processes`) from `run_command`, not a hidden
    `background=True` flag threaded through run_command. Reason: run_command's
    tool spec (what's actually sent to the LLM -- see tools.py TOOL_SPECS) has
    no `mission_id`/`background` parameters, and the LLM can only pass
    arguments that are in that JSON schema. A flag the model can never set is
    a flag that never fires. Giving the model an explicit, separate tool with
    its own clear purpose is the only version of this fix that can actually
    be invoked in practice.
  - It does NOT scope registration to mission_id. Plain run_agent() calls
    (outside run_mission) can also start a dev server that needs cleanup --
    tying this exclusively to missions would miss that case. Instead every
    registered process gets its own opaque handle (a short id), independent
    of whether a mission is active.
  - Registry is a small JSON file (.agent_processes.json) so it survives
    across separate Python process invocations (this agent's own process
    can be restarted while a server it started is still running) -- same
    persistence pattern as checkpoint.py's index.json and missions.py's
    progress.json.
  - atexit-registered best-effort cleanup on normal interpreter exit, PLUS
    an explicit `cleanup_all()`/`cleanup(handle)` the agent can call anytime
    (e.g. at the end of a mission) rather than only relying on atexit, since
    atexit does not fire on SIGKILL or a hard crash.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

STATE_FILE = Path(__file__).parent / ".agent_processes.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _is_alive(pid: int) -> bool:
    """Check if a process with this PID currently exists AND is not a
    zombie waiting to be reaped.

    Real bug found and fixed while testing this module: os.kill(pid, 0)
    returns success (no exception) for a zombie/defunct process too -- the
    PID entry still exists in the process table until something calls
    wait()/waitpid() on it, even though the process has already exited and
    stopped doing any real work. Confirmed directly: after SIGTERM-ing a
    child we are the direct parent of, `ps` showed `<defunct>` / STAT=Z
    while os.kill(pid, 0) still reported it alive, which would have made
    stop() hang waiting for a process that was already gone. Since we are
    always the immediate parent (subprocess.Popen), we're responsible for
    reaping it -- os.waitpid(pid, os.WNOHANG) both reaps AND tells us
    definitively whether it's still running.
    """
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            # Just reaped it -- it had already exited.
            return False
    except ChildProcessError:
        # Not our child (e.g. already reaped earlier, or PID reused) --
        # fall through to a plain kill(0) existence check.
        pass
    except ProcessLookupError:
        return False

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else -- still "alive" from our
        # perspective, just not something we could kill anyway.
        return True


def start(cmd: str, cwd: Optional[str] = None, name: Optional[str] = None) -> dict:
    """
    Launch `cmd` in the background using Popen directly (not shell `&`), so
    we get a real PID back immediately -- confirmed via test that shell `&`
    backgrounding inside subprocess.run() gives no usable PID to the caller
    at all, only to the shell itself.

    start_new_session=True puts the child in its own process group so a
    SIGTERM/SIGKILL to it doesn't need to guess whether the shell around it
    also needs killing, and so it isn't in our own process group (won't get
    signals meant for the agent process itself).

    Returns a dict with a "handle" (short id) the caller uses to
    stop()/query it later, plus the real pid, for transparency.
    """
    handle = uuid.uuid4().hex[:8]
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd or Path.cwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state = _load_state()
    state[handle] = {
        "pid": proc.pid,
        "cmd": cmd,
        "cwd": str(cwd or Path.cwd()),
        "name": name or cmd[:60],
        "start_time": time.time(),
    }
    _save_state(state)
    return {"handle": handle, "pid": proc.pid}


def list_processes() -> dict:
    """Return all tracked processes, pruning entries whose PID is no longer
    alive (e.g. it crashed on its own, or was killed outside our control)."""
    state = _load_state()
    changed = False
    for handle, info in list(state.items()):
        if not _is_alive(info["pid"]):
            del state[handle]
            changed = True
    if changed:
        _save_state(state)
    return state


def stop(handle: str, grace_seconds: float = 2.0) -> str:
    """Gracefully stop a tracked process: SIGTERM, wait, SIGKILL if still
    alive. Removes it from the registry either way (a dead PID isn't worth
    tracking, and if it's already gone there's nothing to clean up)."""
    state = _load_state()
    info = state.get(handle)
    if info is None:
        return f"ERROR: no tracked process with handle {handle!r}"

    pid = info["pid"]
    name = info.get("name", handle)
    result_lines = []
    try:
        # Signal the whole process group (negative pid) since start_new_session
        # made this process its own group leader -- catches child processes
        # it may have spawned (e.g. `npm start` spawning node) too.
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        result_lines.append(f"Sent SIGTERM to {name} (pid {pid})")
    except ProcessLookupError:
        result_lines.append(f"{name} (pid {pid}) was already gone")
        del state[handle]
        _save_state(state)
        return "\n".join(result_lines)
    except PermissionError as e:
        return f"ERROR: no permission to signal pid {pid}: {e}"

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _is_alive(pid):
            break
        time.sleep(0.2)

    if _is_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            result_lines.append(f"Still alive after {grace_seconds}s -- force-killed with SIGKILL")
        except ProcessLookupError:
            pass

    del state[handle]
    _save_state(state)
    result_lines.append(f"Stopped and untracked {name}")
    return "\n".join(result_lines)


def cleanup_all(grace_seconds: float = 2.0) -> str:
    """Stop every currently-tracked process. Intended for atexit and for
    explicit end-of-mission/end-of-task cleanup."""
    state = _load_state()
    if not state:
        return "(no tracked background processes)"
    lines = []
    for handle in list(state.keys()):
        lines.append(stop(handle, grace_seconds=grace_seconds))
    return "\n".join(lines)


def cleanup_orphans_from_previous_run() -> str:
    """On fresh import/startup, kill anything left over in the registry from
    a PREVIOUS process that never cleanly called cleanup_all() (e.g. it
    crashed). Distinguishes from list_processes()'s passive pruning by
    actually killing, not just forgetting, since these are real orphaned
    processes still consuming resources."""
    state = _load_state()
    if not state:
        return "(nothing to clean up)"
    lines = []
    for handle, info in list(state.items()):
        pid = info["pid"]
        if _is_alive(pid):
            lines.append(f"Killing orphaned process from a previous run: {info.get('name', handle)} (pid {pid})")
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        del state[handle]
    _save_state(state)
    return "\n".join(lines) if lines else "(stale entries removed, nothing was still running)"


# ---------------------------------------------------------------------------
# Lifecycle hooks -- deliberately NOT automatic on import
# ---------------------------------------------------------------------------
# An earlier version of this module registered atexit.register(cleanup_all)
# and ran cleanup_orphans_from_previous_run() unconditionally at import time.
# That was WRONG and was caught by direct testing before shipping: in this
# project's actual usage pattern, a background process is routinely started
# by one short-lived process/tool-call and is meant to be used (curled,
# screenshotted, etc.) by LATER, separate tool-call invocations, then
# explicitly stopped -- e.g. the established sandbox pattern of starting a
# Flask/http.server via one `run_command`/background call, then verifying
# against it across several subsequent, separate calls. Reproduced directly:
# starting a server, then merely re-importing this module in a fresh
# process (simulating the next tool call), triggered atexit and killed the
# server before anything could use it -- the exact opposite of the intended
# behavior.
#
# The correct boundary for automatic cleanup is "the AGENT's own process/
# mission lifetime", not "any Python process that happens to import this
# module". So callers -- not this module -- decide when that boundary is:
#   - agent.py's run_mission() calls cleanup_all() explicitly after a
#     mission's task completes (this is the direct fix for the originally
#     reported bug: "Flask servers survive mission exit").
#   - main.py registers atexit.register(process_manager.cleanup_all) itself,
#     once, for the interactive REPL's own real process lifetime (Ctrl-C,
#     /exit, or an uncaught exception all still fire atexit; SIGKILL does
#     not, which is a known, accepted gap -- see cleanup_orphans_from_
#     previous_run below for that case).
#   - cleanup_orphans_from_previous_run() is exposed for callers to invoke
#     explicitly at a meaningful "fresh session starting" boundary (e.g.
#     main.py's startup) -- not on every import -- since it can't tell the
#     difference between "orphaned by a crash" and "still legitimately
#     running, about to be used by the next command" on its own.
