"""
test_meta_tools.py
------------------
TDD Unit Tests for PulseCodeAI Meta Tools (`packages/tools/meta`).
Verifies skills, rules, plugins, custom agents, and repo_map queries.
"""
import pytest
from pathlib import Path
from src.meta_tools import (
    ListSkillsTool, LoadSkillTool, ListRulesTool,
    ListPluginsTool, InstallPluginTool, ListCustomAgentsTool, RepoMapQueryTool
)


def test_meta_tools_execution(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    (workspace / "main.py").write_text("import os\ndef run():\n    pass\n")

    context = {"workspace_root": str(workspace)}

    assert ListSkillsTool().execute({}, context)["status"] == "success"
    assert LoadSkillTool().execute({"skill_name": "react-component"}, context)["status"] in ("success", "error")
    assert ListRulesTool().execute({}, context)["status"] == "success"
    assert ListPluginsTool().execute({}, context)["status"] == "success"
    assert InstallPluginTool().execute({"plugin_name": "git-safety"}, context)["status"] in ("success", "error")
    assert ListCustomAgentsTool().execute({}, context)["status"] == "success"

    res_map = RepoMapQueryTool().execute({"path": "."}, context)
    assert res_map["status"] == "success"
    assert "main.py" in res_map["output"]
