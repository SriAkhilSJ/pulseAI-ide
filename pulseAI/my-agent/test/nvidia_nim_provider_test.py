"""
Regression test for adding NVIDIA NIM (z-ai/glm-5.2) to llm_client.py's
Router fallback chain.

Real bugs/facts verified LIVE before this provider was wired in (see
llm_client.py's _build_router docstring and README.md for the full story):

  1. NVIDIA's own docs/snippets tell you to set `NVIDIA_API_KEY` -- but
     litellm's `nvidia_nim/` provider actually reads `NVIDIA_NIM_API_KEY`
     when no explicit api_key kwarg is passed. Confirmed directly against
     litellm 1.91.0's own source AND with a real live API call (env var
     alone, no explicit api_key) -- NOT assumed from a blog post.
  2. `litellm.completion(model="nvidia_nim/z-ai/glm-5.2", ...)` needs no
     api_base override -- litellm resolves the real
     https://integrate.api.nvidia.com/v1 endpoint internally for this
     provider prefix (confirmed in get_llm_provider_logic.py).
  3. GLM-5.2 supports real OpenAI-style tool-calling (verified with a real
     get_weather tool-call round trip against the live endpoint, both via
     the raw `openai` SDK the user pasted AND via litellm).
  4. Real fallback (Router genuinely calling a DIFFERENT provider, not
     just retrying the same one) verified: with litellm.completion mocked
     to fail every "groq" model call and succeed otherwise, chat_completion
     ends up calling nvidia_nim/z-ai/glm-5.2 next, per the user's chosen
     provider order (groq -> nvidia -> gemini -> cerebras -> openrouter).

This test mocks `litellm.completion` (the function Router calls per
deployment attempt), NOT `router.completion` itself -- an earlier draft of
this test mocked router.completion wholesale, which IS the retry/fallback
logic under test, so the mocked exception propagated immediately instead
of ever exercising real fallback. Real mistake caught before this was
committed as the permanent regression test.

Run with: PYTHONPATH=/home/user/my-agent python3 test/nvidia_nim_provider_test.py
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import litellm  # noqa: E402
import llm_client  # noqa: E402


def test_nvidia_agent_is_registered_when_key_present():
    assert os.getenv("NVIDIA_NIM_API_KEY"), (
        "This test requires a real NVIDIA_NIM_API_KEY in the environment "
        "(.env) -- it exercises the real, live-verified Router wiring, "
        "not a fully mocked stand-in."
    )
    router, first_model = llm_client.get_router()
    model_names = [m["model_name"] for m in router.model_list]
    assert "nvidia-agent" in model_names, f"nvidia-agent missing from {model_names}"
    nvidia_entry = next(m for m in router.model_list if m["model_name"] == "nvidia-agent")
    assert nvidia_entry["litellm_params"]["model"] == "nvidia_nim/z-ai/glm-5.2"
    print("PASS: nvidia-agent registered with the correct litellm model string")


def test_provider_order_matches_user_decision():
    router, first_model = llm_client.get_router()
    model_names = [m["model_name"] for m in router.model_list]
    # User's explicit decision: groq (fastest/proven) first, GLM-5.2
    # (highest quality, less proven under sustained load) second, then the
    # existing chain unchanged.
    expected_prefix = ["groq-agent", "nvidia-agent"]
    assert model_names[:2] == expected_prefix, (
        f"expected groq-agent then nvidia-agent first, got {model_names[:2]}"
    )
    print(f"PASS: provider order is {model_names} (groq -> nvidia -> ...)")


def test_real_fallback_from_groq_to_nvidia():
    """The real mechanism: Router genuinely calls a DIFFERENT provider
    once groq's retries are exhausted, not just retrying groq forever."""
    router, first_model = llm_client.get_router()
    assert first_model == "groq-agent"

    call_log = []
    real_completion = litellm.completion

    def fake_completion(*args, **kwargs):
        model = kwargs.get("model", "")
        call_log.append(model)
        if "groq" in model:
            raise litellm.exceptions.RateLimitError(
                message="simulated rate limit", llm_provider="groq", model=model,
            )
        return real_completion(*args, **kwargs)

    with patch("litellm.completion", side_effect=fake_completion):
        result = llm_client.chat_completion(
            messages=[{"role": "user", "content": "say OK"}],
            timeout_seconds=60,
        )

    assert any("groq" in m for m in call_log), f"groq was never attempted: {call_log}"
    assert any("nvidia_nim" in m for m in call_log), (
        f"never fell back to nvidia_nim after groq failures: {call_log}"
    )
    assert result.choices[0].message.content, "fallback call produced no content"
    print(f"PASS: real fallback groq -> nvidia_nim confirmed, call sequence: {call_log}")


def test_real_live_tool_call_through_nvidia():
    """End-to-end: force the Router's nvidia-agent deployment specifically
    and confirm a real tool-calling round trip works (GLM-5.2 must
    correctly emit an OpenAI-style tool_call, not just plain text)."""
    router, _ = llm_client.get_router()
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    result = router.completion(
        model="nvidia-agent",
        messages=[{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
        tools=tools,
        tool_choice="auto",
        max_tokens=200,
    )
    msg = result.choices[0].message
    assert msg.tool_calls, f"expected a real tool_call from GLM-5.2, got: {msg}"
    assert msg.tool_calls[0].function.name == "get_weather"
    print(f"PASS: real live tool-call through nvidia-agent (GLM-5.2): {msg.tool_calls[0].function}")


if __name__ == "__main__":
    test_nvidia_agent_is_registered_when_key_present()
    test_provider_order_matches_user_decision()
    test_real_fallback_from_groq_to_nvidia()
    test_real_live_tool_call_through_nvidia()
    print("\nALL TESTS PASSED")
