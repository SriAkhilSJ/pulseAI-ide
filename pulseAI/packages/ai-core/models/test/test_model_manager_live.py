"""
test_model_manager_live.py
--------------------------
Live Verification Test (`<real test>`) for PulseCodeAI ModelManager.
Connects over the wire to actual LLM providers using API keys loaded securely from `.env`.
"""
import os
import pytest
from pathlib import Path
from src.model_manager import ModelManager


def _load_env_keys():
    env_path = Path("/home/user/pulseAI_repo/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val


@pytest.mark.live
def test_live_model_manager_completion():
    _load_env_keys()
    assert any(k in os.environ for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "CEREBRAS_API_KEY"))
    
    mgr = ModelManager()
    messages = [
        {"role": "system", "content": "You are a concise AI test assistant."},
        {"role": "user", "content": "Return only the exact word PULSECODE without punctuation or extra text."}
    ]
    
    result = mgr.complete(messages=messages, model="groq/llama-3.3-70b-versatile", max_tokens=10)
    assert result["status"] == "success"
    assert "PULSECODE" in result["content"].upper()
    assert result["usage"].get("completion_tokens", 0) > 0
    print(f"\n[LIVE TEST VERIFIED] Model used: {result['model_used']} | Response: '{result['content']}'")


@pytest.mark.live
def test_live_model_manager_failover():
    _load_env_keys()
    mgr = ModelManager()
    messages = [
        {"role": "user", "content": "Return only the exact word FAILOVER."}
    ]
    
    # We pass an intentionally non-existent/rate-limited primary model mapped to a live fallback
    custom_fallbacks = {
        "groq/non-existent-model-999": [
            "openrouter/meta-llama/llama-3.3-70b-instruct"
        ]
    }
    mgr.fallbacks = custom_fallbacks
    
    result = mgr.complete(messages=messages, model="groq/non-existent-model-999", max_tokens=10)
    assert result["status"] == "success"
    assert "FAILOVER" in result["content"].upper()
    assert result["failover_occurred"] is True
    assert result["model_used"] == "openrouter/meta-llama/llama-3.3-70b-instruct"
    print(f"\n[LIVE FAILOVER VERIFIED] Primary failed cleanly -> Fallback used: {result['model_used']} | Response: '{result['content']}'")
