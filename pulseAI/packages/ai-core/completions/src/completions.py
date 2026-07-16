"""
completions.py
--------------
PulseCodeAI GhostText Inline Completion Engine (`packages/ai-core/completions`).
Predicts next code insertions via Fill-In-The-Middle (`FIM`) prompts and caches recent keystrokes.
"""
import time
from typing import Any, Dict, Optional


class FimCompletionEngine:
    """Inline code completion using FIM tokens `<|fim_prefix|>...<|fim_suffix|>...<|fim_middle|>` and LRU caching."""

    def __init__(self, model_manager: Optional[Any] = None, cache_ttl_secs: float = 3.0):
        self.model_manager = model_manager
        self.cache_ttl_secs = cache_ttl_secs
        self._cache: Dict[str, Dict[str, Any]] = {}

    def build_prompt(self, file_content: str, cursor_offset: int, max_prefix_chars: int = 1500, max_suffix_chars: int = 500) -> str:
        """Split content around cursor_offset and construct standard FIM completion prompt."""
        safe_offset = max(0, min(cursor_offset, len(file_content)))
        prefix = file_content[:safe_offset][-max_prefix_chars:]
        suffix = file_content[safe_offset:][:max_suffix_chars]
        return f"<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>"

    def predict(self, file_path: str, content: str, cursor_offset: int, model: str = "groq/llama-3.1-8b-instant") -> Dict[str, Any]:
        """Request inline completion for target keystroke position, returning cached result if within TTL."""
        prompt = self.build_prompt(content, cursor_offset)
        cache_key = f"{file_path}:{len(content)}:{cursor_offset}:{prompt[-60:]}"

        now = time.time()
        if cache_key in self._cache:
            entry = self._cache[cache_key]
            if now - entry["timestamp"] <= self.cache_ttl_secs:
                return {
                    "status": "success",
                    "completion": entry["completion"],
                    "cached": True,
                    "model_used": entry.get("model_used", model)
                }

        if not self.model_manager:
            return {"status": "error", "output": "ModelManager not attached to FimCompletionEngine."}

        messages = [{"role": "user", "content": prompt}]
        try:
            res = self.model_manager.complete(messages=messages, model=model, max_tokens=50, temperature=0.1)
            completion_text = res.get("content", "").strip()
            
            # Clean up trailing markdown or duplicate indentation
            if completion_text.startswith("```"):
                lines = completion_text.splitlines()
                if len(lines) > 1 and lines[-1].strip() == "```":
                    completion_text = "\n".join(lines[1:-1])
                elif len(lines) > 1:
                    completion_text = "\n".join(lines[1:])

            self._cache[cache_key] = {
                "completion": completion_text,
                "timestamp": now,
                "model_used": res.get("model_used", model)
            }
            return {
                "status": "success",
                "completion": completion_text,
                "cached": False,
                "model_used": res.get("model_used", model)
            }
        except Exception as exc:
            return {"status": "error", "output": f"CompletionPredictionError: {exc}"}
