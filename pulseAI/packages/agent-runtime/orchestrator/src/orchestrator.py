"""
orchestrator.py
---------------
PulseCodeAI Multi-Agent Orchestrator (`packages/agent-runtime/orchestrator`).
Drives ReAct loops, enforces sub-agent allowlist boundaries, and checkpoint recovery via MissionManager.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MissionManager:
    """Manages atomic checkpoints for long-running agent tasks across crashes and restarts."""

    def __init__(self, workspace_root: str = "."):
        self.workspace_root = Path(workspace_root).resolve()
        self.missions_dir = self.workspace_root / ".pulsecode" / "missions"
        self.missions_dir.mkdir(parents=True, exist_ok=True)

    def save_checkpoint(self, mission_id: str, turn_number: int, messages: List[Dict[str, Any]], status: str = "in_progress") -> Path:
        mission_folder = self.missions_dir / mission_id
        mission_folder.mkdir(parents=True, exist_ok=True)
        checkpoint_file = mission_folder / "checkpoint.json"

        data = {
            "mission_id": mission_id,
            "turn_number": turn_number,
            "messages": messages,
            "status": status
        }
        temp_file = mission_folder / "checkpoint.json.tmp"
        temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_file.replace(checkpoint_file)
        return checkpoint_file

    def load_checkpoint(self, mission_id: str) -> Optional[Dict[str, Any]]:
        checkpoint_file = self.missions_dir / mission_id / "checkpoint.json"
        if not checkpoint_file.exists():
            return None
        try:
            return json.loads(checkpoint_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"Failed to load checkpoint for {mission_id}: {exc}")
            return None


class AgentOrchestrator:
    """The core multi-turn ReAct loop driving tools, models, checkpoints, and sub-agent dispatching."""

    def __init__(self, model_manager: Any, tool_registry: Any, mission_manager: Optional[MissionManager] = None):
        self.model_manager = model_manager
        self.tool_registry = tool_registry
        self.mission_manager = mission_manager or MissionManager()

    def run_agent_loop(
        self,
        prompt: str,
        model: str,
        allowed_tools: Optional[List[str]] = None,
        permission_mode: str = "normal",
        mission_id: Optional[str] = None,
        max_turns: int = 15
    ) -> Dict[str, Any]:
        """Execute the multi-turn ReAct cycle until task completion or step limit."""
        messages = [{"role": "user", "content": prompt}]
        turns_executed = 0

        # Build OpenAI-compatible tool schemas for the active allowlist
        tool_specs = None
        if hasattr(self.tool_registry, "list_tools_schema"):
            tool_specs = self.tool_registry.list_tools_schema(allowed_tools)

        while turns_executed < max_turns:
            turns_executed += 1
            
            # Call ModelManager with tools
            completion_kwargs = {}
            if tool_specs:
                completion_kwargs["tools"] = tool_specs
                completion_kwargs["tool_choice"] = "auto"

            res = self.model_manager.complete(messages=messages, model=model, **completion_kwargs)
            raw_resp = res.get("raw_response")
            
            # Check for tool calls
            tool_calls = None
            if raw_resp and hasattr(raw_resp, "choices") and raw_resp.choices:
                tool_calls = getattr(raw_resp.choices[0].message, "tool_calls", None)

            if not tool_calls:
                # Task complete
                final_text = res.get("content", "")
                if mission_id:
                    self.mission_manager.save_checkpoint(mission_id, turns_executed, messages, status="completed")
                return {
                    "status": "success",
                    "final_answer": final_text,
                    "turns_executed": turns_executed,
                    "mission_id": mission_id
                }

            # Process every tool call
            for tc in tool_calls:
                fn_name = getattr(tc.function, "name", "")
                fn_args_str = getattr(tc.function, "arguments", "{}")
                try:
                    fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                except Exception:
                    fn_args = {}

                # Check sub-agent allowlist isolation
                if allowed_tools is not None and fn_name not in allowed_tools:
                    obs_content = f"ERROR: unknown tool '{fn_name}'. Allowed tools for this agent role are: {', '.join(allowed_tools)}"
                else:
                    # Save checkpoint before any mutating execution if tracking mission
                    if mission_id and getattr(self.tool_registry.tools.get(fn_name), "is_mutating", False):
                        self.mission_manager.save_checkpoint(mission_id, turns_executed, messages, status="in_progress")

                    # Execute tool via UnifiedToolRegistry
                    exec_res = self.tool_registry.execute(fn_name, fn_args, context={"permission_mode": permission_mode})
                    obs_content = exec_res.get("output", str(exec_res))

                messages.append({
                    "role": "assistant",
                    "content": getattr(raw_resp.choices[0].message, "content", None),
                    "tool_calls": [tc]
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", "call_default"),
                    "name": fn_name,
                    "content": str(obs_content)
                })

        return {
            "status": "error",
            "output": f"Exceeded max_turns ({max_turns}) without final answer.",
            "turns_executed": turns_executed
        }


class DispatchAgentTool:
    """Tool allowing parent agents to delegate self-contained tasks to restricted sub-agents."""
    name = "dispatch_agent"
    description = "Dispatch a specialized sub-agent on an isolated sub-task with restricted tool permissions."
    is_mutating = False

    def __init__(self, orchestrator: Optional[AgentOrchestrator] = None):
        self.orchestrator = orchestrator

    def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.orchestrator:
            return {"status": "error", "output": "Orchestrator not attached to DispatchAgentTool."}

        sub_prompt = args.get("prompt", "")
        sub_type = args.get("subagent_type", "Explore")
        model = args.get("model", "groq/llama-3.3-70b-versatile")

        if not sub_prompt:
            return {"status": "error", "output": "Missing required parameter: 'prompt'"}

        allowlists = {
            "Explore": ["filesystem_read_file", "grep_files", "git_status", "rag_search", "lsp_get_diagnostics"],
            "Plan": ["filesystem_read_file", "grep_files", "repo_map_query", "rag_search"],
            "Coder": ["filesystem_read_file", "filesystem_write_file", "apply_edit", "lsp_get_diagnostics", "git_diff", "run_command"]
        }
        allowed = allowlists.get(sub_type, allowlists["Explore"])

        res = self.orchestrator.run_agent_loop(
            prompt=sub_prompt,
            model=model,
            allowed_tools=allowed,
            permission_mode=context.get("permission_mode", "normal")
        )

        if res["status"] == "success":
            return {"status": "success", "output": f"Sub-Agent ({sub_type}) Summary:\n{res.get('final_answer', '')}"}
        return {"status": "error", "output": f"Sub-Agent failed: {res.get('output', '')}"}
