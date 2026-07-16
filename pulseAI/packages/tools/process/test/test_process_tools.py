"""
test_process_tools.py
---------------------
TDD Unit Tests for PulseCodeAI Background Process Manager (`packages/tools/process`).
Verifies starting, listing, and stopping persistent background processes.
"""
import time
import pytest
from src.process_tools import ProcessManagerTool


def test_process_manager_start_list_stop():
    tool = ProcessManagerTool()
    context = {"workspace_root": "."}
    
    # Start long process
    res_start = tool.execute({"action": "start", "command": "python3 -c 'import time; time.sleep(10)'", "process_id": "test_sleep"}, context)
    assert res_start["status"] == "success"
    assert "started" in res_start["output"].lower()
    
    # List processes
    res_list = tool.execute({"action": "list"}, context)
    assert res_list["status"] == "success"
    assert "test_sleep" in res_list["output"]
    
    # Stop process
    res_stop = tool.execute({"action": "stop", "process_id": "test_sleep"}, context)
    assert res_stop["status"] == "success"
    assert "stopped" in res_stop["output"].lower()
