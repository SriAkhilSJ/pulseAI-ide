"""
test_model_manager.py
---------------------
TDD Unit Tests for PulseCodeAI ai-core ModelManager & TokenManager.
Verifies automatic circuit breaker failover and accurate token accounting.
"""
import pytest
from unittest.mock import patch, MagicMock
from src.model_manager import ModelManager, TokenManager, CircuitBreakerError


def test_token_manager_estimate():
    messages = [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Write a python function to add two numbers."}
    ]
    tokens = TokenManager.estimate_tokens(messages, model="groq/llama-3.3-70b-versatile")
    assert isinstance(tokens, int)
    assert tokens > 15 and tokens < 50


def test_model_manager_fallback_mapping():
    mgr = ModelManager()
    fallbacks = mgr.get_fallback_models("groq/llama-3.3-70b-versatile")
    assert "openrouter/meta-llama/llama-3.3-70b-instruct" in fallbacks or len(fallbacks) > 0

    fallbacks_claude = mgr.get_fallback_models("claude-3-5-sonnet-20241022")
    assert "openrouter/anthropic/claude-3.5-sonnet" in fallbacks_claude


@patch("src.model_manager.litellm")
def test_model_manager_circuit_breaker_failover(mock_litellm):
    # Setup mock: primary raises RateLimitError, fallback succeeds
    class FakeRateLimitError(Exception):
        pass

    mock_litellm.RateLimitError = FakeRateLimitError
    mock_litellm.APIConnectionError = Exception

    # Primary call fails with 429 RateLimitError
    # Fallback call succeeds
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="Hello from fallback model!"))]
    mock_response.model = "openrouter/meta-llama/llama-3.3-70b-instruct"
    mock_response.usage.prompt_tokens = 20
    mock_response.usage.completion_tokens = 6

    mock_litellm.completion.side_effect = [
        FakeRateLimitError("429 Too Many Requests from Groq API"),
        mock_response
    ]

    mgr = ModelManager()
    messages = [{"role": "user", "content": "hello"}]
    
    result = mgr.complete(messages=messages, model="groq/llama-3.3-70b-versatile")
    
    assert result["status"] == "success"
    assert result["content"] == "Hello from fallback model!"
    assert result["model_used"] == "openrouter/meta-llama/llama-3.3-70b-instruct"
    assert result["failover_occurred"] is True
    assert mock_litellm.completion.call_count == 2
