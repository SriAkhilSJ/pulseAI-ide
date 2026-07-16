"""
process_tools.py
----------------
PulseCodeAI Sandboxed Tool System — Background Process Manager (`packages/tools/process`).
Migrates process_manager into sandboxed tools.
"""
import subprocess
from typing import Any, Dict


class BaseTool:
    name: str = ""
    description: str = ""
    is_mutating: bool = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError()


class ProcessManagerTool(BaseTool):
    name = "process_manager"
    description = "Start, list, or stop background daemon processes."
    is_mutating = True

    _running_processes: Dict[str, subprocess.Popen] = {}

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action", "")
        if action == "start":
            cmd = args.get("command", "")
            pid_key = args.get("process_id", "proc_1")
            if not cmd:
                return {"status": "error", "output": "Missing command for start action."}
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._running_processes[pid_key] = proc
            return {"status": "success", "output": f"Started background process '{pid_key}' with system pid {proc.pid}."}

        elif action == "list":
            active = []
            for pid_key, proc in list(self._running_processes.items()):
                if proc.poll() is None:
                    active.append(f"- {pid_key} (PID: {proc.pid})")
                else:
                    del self._running_processes[pid_key]
            if not active:
                return {"status": "success", "output": "No active background processes."}
            return {"status": "success", "output": "Active background processes:\n" + "\n".join(active)}

        elif action == "stop":
            pid_key = args.get("process_id", "")
            if pid_key not in self._running_processes:
                return {"status": "error", "output": f"Process '{pid_key}' not found or already terminated."}
            proc = self._running_processes.pop(pid_key)
            proc.terminate()
            return {"status": "success", "output": f"Stopped background process '{pid_key}'."}

        return {"status": "error", "output": f"Unknown process action: '{action}'"}
