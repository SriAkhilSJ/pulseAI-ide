"""
model_manager.py
----------------
PulseCodeAI Multi-Provider Model Manager & Token Accounting Engine.
Implements automatic circuit breaker failover and exact/estimated token tracking.
"""
import logging
from typing import Any, Dict, List

try:
    import litellm
except ImportError:
    litellm = None

logger = logging.getLogger(__name__)


class CircuitBreakerError(Exception):
    """Raised when primary and all fallback models fail or exceed rate limits."""
    pass


class TokenManager:
    """Estimates and tracks token consumption across multi-turn sessions."""

    @staticmethod
    def estimate_tokens(messages: List[Dict[str, str]], model: str = "") -> int:
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        total_chars += len(block.get("text", ""))
        return max(1, (total_chars // 4) + (len(messages) * 4))


class ModelManager:
    """Multi-provider LLM routing with automatic circuit-breaker failover."""

    DEFAULT_FALLBACKS = {
        "groq/llama-3.3-70b-versatile": [
            "openrouter/meta-llama/llama-3.3-70b-instruct",
            "groq/llama-3.1-8b-instant"
        ],
        "claude-3-5-sonnet-20241022": [
            "openrouter/anthropic/claude-3.5-sonnet",
            "groq/llama-3.3-70b-versatile"
        ],
        "gpt-4o": [
            "openrouter/openai/gpt-4o",
            "openrouter/meta-llama/llama-3.3-70b-instruct"
        ]
    }

    def __init__(self, fallbacks: Dict[str, List[str]] | None = None):
        self.fallbacks = fallbacks if fallbacks is not None else self.DEFAULT_FALLBACKS.copy()

    def get_fallback_models(self, primary_model: str) -> List[str]:
        return self.fallbacks.get(primary_model, [
            "openrouter/meta-llama/llama-3.3-70b-instruct",
            "groq/llama-3.1-8b-instant"
        ])

    def complete(self, messages: List[Dict[str, Any]], model: str, **kwargs) -> Dict[str, Any]:
        if litellm is None:
            raise RuntimeError("litellm package is required for ModelManager.complete()")

        models_to_try = [model] + [fb for fb in self.get_fallback_models(model) if fb != model]
        last_error = None
        failover_occurred = False

        for idx, current_model in enumerate(models_to_try):
            try:
                if idx > 0:
                    logger.warning(f"Circuit breaker triggered for {model}. Failing over to {current_model}...")
                    failover_occurred = True

                response = litellm.completion(model=current_model, messages=messages, **kwargs)
                
                content = ""
                if response.choices and len(response.choices) > 0:
                    content = response.choices[0].message.content or ""

                usage = {}
                if hasattr(response, "usage") and response.usage:
                    usage = {
                        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                    }

                return {
                    "status": "success",
                    "content": content,
                    "model_used": current_model,
                    "failover_occurred": failover_occurred,
                    "usage": usage,
                    "raw_response": response
                }

            except Exception as exc:
                exc_type_name = type(exc).__name__
                if any(k in exc_type_name for k in ("RateLimit", "Connection", "API", "Timeout", "ServiceUnavailable", "NotFound", "InternalServer", "BadRequest")):
                    last_error = exc
                    continue
                else:
                    raise exc

        raise CircuitBreakerError(f"All models exhausted for request. Last error: {last_error}")
