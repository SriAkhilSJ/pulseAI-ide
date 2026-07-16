"""
context_tools.py
----------------
PulseCodeAI AI Core — Context Manager & Auto-Compression Engine.
Enforces token budgeting and auto-summarizes older conversation history.
"""
import os
from pathlib import Path
from typing import Any, Dict, List


class ContextCompressor:
    """Monitors token consumption and auto-compacts conversation history when approaching limits."""

    def __init__(self, max_tokens: int = 128000, threshold_ratio: float = 0.8):
        self.max_tokens = max_tokens
        self.threshold_ratio = threshold_ratio

    @staticmethod
    def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return max(1, (total_chars // 4) + (len(messages) * 4))

    def should_compress(self, messages: List[Dict[str, Any]]) -> bool:
        return self.estimate_tokens(messages) > (self.max_tokens * self.threshold_ratio)

    def compress(self, messages: List[Dict[str, Any]], keep_recent_turns: int = 6) -> List[Dict[str, Any]]:
        """Compact older turns into a single summary block while preserving system prompt and recent turns."""
        if not messages or len(messages) <= (keep_recent_turns * 2) + 1:
            return messages

        system_messages = [m for m in messages if m.get("role") == "system"]
        conversation_messages = [m for m in messages if m.get("role") != "system"]

        if len(conversation_messages) <= keep_recent_turns * 2:
            return messages

        # Split into old turns to compact and recent turns to preserve
        cutoff_index = len(conversation_messages) - (keep_recent_turns * 2)
        old_turns = conversation_messages[:cutoff_index]
        recent_turns = conversation_messages[cutoff_index:]

        # Build summary of old turns
        summary_lines = ["Compacted History Summary of older turns:"]
        for turn in old_turns:
            role = turn.get("role", "unknown")
            content = str(turn.get("content", "")).strip()
            # Truncate long individual turns for the summary
            short_content = content[:150] + ("..." if len(content) > 150 else "")
            summary_lines.append(f"- [{role}]: {short_content}")

        summary_message = {
            "role": "system",
            "content": "\n".join(summary_lines)
        }

        # Reassemble: System messages + Summary + Recent turns
        return system_messages + [summary_message] + recent_turns


class ContextManager:
    """Inspects workspace structure and generates token-budgeted dependency maps."""

    def __init__(self, workspace_root: str = "."):
        self.workspace_root = Path(workspace_root).resolve()

    def get_workspace_map(self, token_budget: int = 2048) -> str:
        """Walk workspace files and format top-down file list with basic signatures under budget."""
        lines = [f"Codebase Map for {self.workspace_root.name}:"]
        current_chars = len(lines[0])
        char_budget = token_budget * 4

        for root, dirs, files in os.walk(self.workspace_root):
            # Exclude hidden/build directories
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "out", "__pycache__", "build")]
            for file in sorted(files):
                if file.startswith(".") or file.endswith((".pyc", ".o", ".exe")):
                    continue
                file_path = Path(root) / file
                rel_path = file_path.relative_to(self.workspace_root)
                entry = f"\n- {rel_path}"
                if current_chars + len(entry) > char_budget:
                    lines.append("\n[... additional files truncated to stay within token budget ...]")
                    return "".join(lines)
                lines.append(entry)
                current_chars += len(entry)

        return "".join(lines)
