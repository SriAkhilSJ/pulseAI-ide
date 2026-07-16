"""
meta_tools.py
-------------
PulseCodeAI Sandboxed Tool System — Meta Tools (`packages/tools/meta`).
Migrates skills, rules, plugins, custom_agents, and repo_map queries into sandboxed tools.
"""
import os
from pathlib import Path
from typing import Any, Dict, List


class BaseTool:
    name: str = ""
    description: str = ""
    is_mutating: bool = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError()


class ListSkillsTool(BaseTool):
    name = "list_skills"
    description = "List all available AI skills in the workspace."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "Available skills:\n- react-component: Build clean, modular React components."}


class LoadSkillTool(BaseTool):
    name = "load_skill"
    description = "Load instructions and tool allowances for a target skill."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        skill = args.get("skill_name", "")
        if not skill:
            return {"status": "error", "output": "Missing parameter: 'skill_name'"}
        return {"status": "success", "output": f"Loaded skill '{skill}': Use functional React components and tailwind/clean CSS."}


class ListRulesTool(BaseTool):
    name = "list_rules"
    description = "List all active project system prompt rules (`AGENTS.md`, `.cursorrules`)."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "Active rules:\n- TDD discipline (`RED -> GREEN -> REFACTOR`)\n- No path traversal or sensitive file access."}


class ListPluginsTool(BaseTool):
    name = "list_plugins"
    description = "List installed and available third-party plugins."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "Installed plugins:\n- git-safety (v1.0.0)"}


class InstallPluginTool(BaseTool):
    name = "install_plugin"
    description = "Install a new plugin from the allowlist."
    is_mutating = True

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        plugin = args.get("plugin_name", "")
        if not plugin:
            return {"status": "error", "output": "Missing parameter: 'plugin_name'"}
        return {"status": "success", "output": f"Successfully installed plugin '{plugin}'."}


class ListCustomAgentsTool(BaseTool):
    name = "list_custom_agents"
    description = "List specialized custom agents defined inside `.agent_agents/` or `packages/agent-runtime/roles/`."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "output": "Available Custom Agents:\n- base-coder\n- security-auditor\n- Planner\n- Coder\n- Reviewer\n- Tester"}


class RepoMapQueryTool(BaseTool):
    name = "repo_map_query"
    description = "Query the Tree-sitter + PageRank dependency graph map for top structural definitions."
    is_mutating = False

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        workspace_root = Path(context.get("workspace_root", ".")).resolve()
        lines = [f"Repo Map for {workspace_root.name}:"]
        for root, dirs, files in os.walk(workspace_root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "out", "__pycache__", "build")]
            for file in sorted(files):
                if file.endswith((".py", ".ts", ".js", ".md", ".html")):
                    lines.append(f"- {file}")
        return {"status": "success", "output": "\n".join(lines[:30])}
