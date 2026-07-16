"""
test_studio_e2e_live.py
-----------------------
Live `<real test>` Verification of PulseCodeAI Multi-Agent Runtime on PulseAI Studio (`apps/web-showcase`).
Connects over the wire to cloud LLMs to read, analyze, and surgically modify the live HTML dashboard using sandboxed tools.
"""
import os
import sys
import pytest
from pathlib import Path

repo_root = Path(__file__).resolve().parents[3]
for sub in ("packages/tools/registry/src", "packages/ai-core/models/src", "packages/agent-runtime/orchestrator/src"):
    p = repo_root / sub
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from unified_registry import UnifiedToolRegistry
from model_manager import ModelManager
from orchestrator import AgentOrchestrator


def _load_env_keys():
    env_path = repo_root / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val


@pytest.mark.live
def test_live_agent_studio_modification():
    _load_env_keys()
    assert any(k in os.environ for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY"))

    workspace = Path(__file__).resolve().parent.parent / "src"
    model_mgr = ModelManager()
    registry = UnifiedToolRegistry(workspace_root=str(workspace))
    orchestrator = AgentOrchestrator(model_manager=model_mgr, tool_registry=registry)

    # Clear instructions so the LLM reads index.html, preserves its exact body, and appends the comment tag before </body>
    prompt = (
        "You are CoderAgent. You must use function calling (`filesystem_read_file` and `filesystem_write_file`).\n"
        "Step 1: Call `filesystem_read_file` with argument `{\"path\": \"index.html\"}` to inspect the current HTML.\n"
        "Step 2: Once you see the exact content of index.html from Step 1, take that ENTIRE HTML text, find the tag `</body>` near the bottom, and insert the exact line `<!-- PULSE_STUDIO_E2E_VERIFIED -->` immediately above `</body>`. Then call `filesystem_write_file` with `{\"path\": \"index.html\", \"content\": \"<entire updated HTML string here>\"}`.\n"
        "Step 3: After `filesystem_write_file` succeeds, reply with 'E2E WEBSITE MODIFICATION COMPLETE'."
    )

    result = orchestrator.run_agent_loop(
        prompt=prompt,
        model="groq/llama-3.3-70b-versatile",
        allowed_tools=["filesystem_read_file", "filesystem_write_file"],
        permission_mode="dont_ask",
        max_turns=6
    )

    assert result["status"] == "success"
    assert "COMPLETE" in result["final_answer"].upper() or "VERIFIED" in result["final_answer"].upper()
    
    # Verify the actual disk file was modified by the live agent
    modified_html = (workspace / "index.html").read_text(encoding="utf-8")
    assert "<!-- PULSE_STUDIO_E2E_VERIFIED -->" in modified_html
    print(f"\n[LIVE E2E STUDIO VERIFIED] Turns executed: {result['turns_executed']} | Comment successfully injected into index.html!")
