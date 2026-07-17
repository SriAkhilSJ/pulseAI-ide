"""
llm_client.py
-------------
Central LLM access point for the agent, built on LiteLLM so we can freely
mix free-tier providers (Groq, NVIDIA NIM, Gemini, Cerebras, OpenRouter) and
get automatic failover when one is rate-limited or down — without changing
any calling code elsewhere in the project.

SECURITY: this file never hardcodes API keys. It only reads them from
environment variables via os.getenv(...). Set the real values in your shell
(or a .env file that is git-ignored) before running anything:

    export GROQ_API_KEY="..."
    export NVIDIA_NIM_API_KEY="..."   # from build.nvidia.com -- NOTE the
                                        # env var name litellm's nvidia_nim/
                                        # provider actually reads is
                                        # NVIDIA_NIM_API_KEY, not
                                        # NVIDIA_API_KEY (the name NVIDIA's
                                        # own docs/snippets use) -- verified
                                        # directly against litellm's source,
                                        # not assumed.
    export GOOGLE_API_KEY="..."
    export CEREBRAS_API_KEY="..."
    export OPENROUTER_API_KEY="..."

If you ever pasted a key into a chat, doc, or committed it to git, treat it
as compromised and regenerate a fresh one from the provider's dashboard.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Optional

from dotenv import load_dotenv
from litellm import completion, Router
from litellm.router import RetryPolicy

# Load key=value pairs from a local .env file (if present) into os.environ,
# without overriding any keys already exported in the real shell environment.
# .env is git-ignored — see .env.example for the template.
load_dotenv(override=False)

# ---------------------------------------------------------------------------
# 1) The "vibecoder" helper — one prompt in, text out, with manual fallback.
#    Good for quick scripts / Phase 1. Change `model` to switch providers.
# ---------------------------------------------------------------------------

# Ordered fallback chain: tried in this order until one succeeds.
# Only free-tier, currently-supported model IDs (checked against each
# provider's docs) — Cerebras and OpenRouter free tiers require models that
# explicitly support tool/function calling, which matters once this feeds
# the agent's ReAct loop.
FALLBACK_CHAIN = [
    "gemini/gemini-2.5-flash",               # Google, free tier, 1M context, supports tools
    "groq/llama-3.3-70b-versatile",          # fast, free, supports tools (12K TPM limit)
    "cerebras/gpt-oss-120b",                 # Cerebras, free tier, supports tools
    "openrouter/openai/gpt-oss-120b:free",   # OpenRouter, free tier, supports tools
]


def ask_llm(prompt: str, model: Optional[str] = None, _chain: Optional[list] = None) -> str:
    """
    Just change the model string to switch providers:
      - "gemini/gemini-2.5-flash"             (Google, free tier, 1M context)
      - "groq/llama-3.3-70b-versatile"        (fast, free, 12K TPM limit)
      - "cerebras/gpt-oss-120b"               (Cerebras, free tier, very fast)
      - "openrouter/openai/gpt-oss-120b:free" (OpenRouter, free tier)

    If a provider errors out (rate limit, outage, missing key, etc.) this
    automatically retries with the next provider in FALLBACK_CHAIN.
    """
    chain = _chain if _chain is not None else FALLBACK_CHAIN
    model = model or chain[0]

    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        return response.choices[0].message.content
    except Exception as e:
        remaining = [m for m in chain if m != model]
        if not remaining:
            raise
        print(f"[llm_client] {model} failed ({e}); falling back to {remaining[0]}...")
        return ask_llm(prompt, model=remaining[0], _chain=remaining)


# ---------------------------------------------------------------------------
# 2) Router — rate-limit insurance for the agent's multi-turn, tool-calling
#    ReAct loop (Phase 2). Every provider with a key set is registered under
#    the same model_name ("code-agent"), so Router can fail over between
#    them mid-conversation without the caller (agent.py) knowing or caring.
# ---------------------------------------------------------------------------

def _build_router() -> Router:
    """
    Build a LiteLLM Router with one model_group PER available provider
    (not one shared "code-agent" group). This distinction matters a lot in
    practice: reproduced live that Groq occasionally emits a malformed
    tool-call that surfaces as a 400 BadRequestError, and confirmed
    empirically (mocked test) that with all providers sharing a single
    model_name, Router's retry logic can retry the SAME failing deployment
    twice and then give up -- it never even tries Gemini/Cerebras/
    OpenRouter, because "retries" and "fallback to a different deployment"
    are different mechanisms in LiteLLM's Router, not the same thing.

    Giving each provider its own model_name and wiring an explicit
    `fallbacks=` chain guarantees that once a provider's retries are
    exhausted, the NEXT call in the chain is a genuinely different
    provider -- verified below with the same mocked "Groq always fails"
    scenario that exposed the original bug.
    """
    model_list = []
    order: list[str] = []  # preserves priority: first configured = tried first

    if os.getenv("GOOGLE_API_KEY"):
        model_list.append({
            "model_name": "gemini-agent",
            "litellm_params": {
                "model": "gemini/gemini-2.5-flash",
                "api_key": os.getenv("GOOGLE_API_KEY"),
            },
        })
        order.append("gemini-agent")
    if os.getenv("GROQ_API_KEY"):
        model_list.append({
            "model_name": "groq-agent",
            "litellm_params": {
                "model": "groq/llama-3.3-70b-versatile",
                "api_key": os.getenv("GROQ_API_KEY"),
            },
        })
        order.append("groq-agent")
    if os.getenv("NVIDIA_NIM_API_KEY"):
        # NVIDIA NIM (build.nvidia.com) hosts Z.ai's GLM-5.2 for free --
        # verified live (real chat_completion + real tool-calling round
        # trip, both against the real endpoint) before wiring this in.
        # GLM-5.2 benchmarks near Claude Opus 4.8 on SWE-Bench Pro, clearly
        # stronger than every other free-tier model in this chain, but its
        # real-world latency/rate-limit behavior under sustained use is NOT
        # yet proven the way Groq's has been across this whole project --
        # placed second (after Groq, the fastest/most battle-tested
        # provider for quick turns) rather than first, per explicit user
        # decision. litellm's native `nvidia_nim/` provider prefix only
        # needs NVIDIA_NIM_API_KEY -- no api_base override required, unlike
        # Cerebras below (confirmed: litellm resolves the real
        # https://integrate.api.nvidia.com/v1 base internally).
        model_list.append({
            "model_name": "nvidia-agent",
            "litellm_params": {
                "model": "nvidia_nim/z-ai/glm-5.2",
                "api_key": os.getenv("NVIDIA_NIM_API_KEY"),
            },
        })
        order.append("nvidia-agent")
    if os.getenv("CEREBRAS_API_KEY"):
        model_list.append({
            "model_name": "cerebras-agent",
            "litellm_params": {
                "model": "cerebras/gpt-oss-120b",
                "api_key": os.getenv("CEREBRAS_API_KEY"),
                "api_base": "https://api.cerebras.ai/v1",
                "extra_headers": {"X-Cerebras-3rd-Party-Integration": "litellm"},
            },
        })
        order.append("cerebras-agent")
    if os.getenv("OPENROUTER_API_KEY"):
        model_list.append({
            "model_name": "openrouter-agent",
            "litellm_params": {
                "model": "openrouter/openai/gpt-oss-120b:free",
                "api_key": os.getenv("OPENROUTER_API_KEY"),
            },
        })
        order.append("openrouter-agent")

    if not model_list:
        raise RuntimeError(
            "No provider API keys found. Set at least one of GROQ_API_KEY, "
            "GOOGLE_API_KEY, CEREBRAS_API_KEY, OPENROUTER_API_KEY as an "
            "environment variable before starting the agent."
        )

    # Explicit fallback chain: gemini -> groq -> nvidia -> cerebras -> openrouter (in
    # whatever order the configured keys give us). Each entry says "if THIS
    # model_group's retries are exhausted, move to these model_groups next".
    # Built as a chain (each provider falls back to everything after it) so
    # it still works correctly regardless of which subset of keys is set.
    fallbacks = [
        {order[i]: order[i + 1:]}
        for i in range(len(order) - 1)
    ]

    return Router(
        model_list=model_list,
        fallbacks=fallbacks,
        num_retries=1,          # retries on the SAME deployment before moving to fallbacks
        allowed_fails=1,        # 1 failure knocks a deployment into cooldown
        cooldown_time=30,       # seconds a failed deployment sits out
        routing_strategy="simple-shuffle",
        # By default LiteLLM treats 400 BadRequestError as "the request is
        # malformed, retrying elsewhere won't help" and does NOT retry it at
        # all. In practice, some providers (e.g. Groq) occasionally emit a
        # malformed tool-call (invalid JSON/XML-ish function syntax) that
        # surfaces as a 400 "tool_use_failed" -- that's a provider quirk,
        # not a genuinely malformed request, so opt in to retrying/failing
        # over on BadRequestError too, alongside other transient error types.
        retry_policy=RetryPolicy(
            BadRequestErrorRetries=1,
            RateLimitErrorRetries=2,
            TimeoutErrorRetries=2,
            InternalServerErrorRetries=2,
            AuthenticationErrorRetries=0,  # bad key won't fix itself, don't waste a retry
        ),
    )


_router: Optional[Router] = None
_router_first_model: Optional[str] = None


def get_router() -> tuple[Router, str]:
    """Lazily build (and cache) the Router, returning (router, first_model_name)
    so callers know which model_group to start the fallback chain from."""
    global _router, _router_first_model
    if _router is None:
        _router = _build_router()
        _router_first_model = _router.model_list[0]["model_name"]
    return _router, _router_first_model


# ---------------------------------------------------------------------------
# 3) Overall wall-clock deadline for chat_completion().
#
# Real bug this fixes (found live during the 3-mission stress test, diagnosed
# with py-spy): when a provider (Groq) got put into a long cooldown --
# litellm.types.router.RouterRateLimitError reported cooldown_time=2185
# (36+ minutes, most likely a daily-quota 429, not a per-minute one) -- the
# Router kept retrying/falling back internally for a legitimately long time
# before the OTHER providers' short 30s cooldowns expired and one of them
# finally answered. Net effect: one chat_completion() call blocked the whole
# ReAct loop for ~40 minutes with zero visibility to the caller.
#
# What did NOT work / was rejected: an earlier draft of this fix tried to
# subclass Router and inspect RouterRateLimitError.cooldown_list expecting a
# list of {"model_name":..., "cooldown_time":...} dicts. Verified against the
# actual installed litellm source (litellm/router_utils/handle_error.py,
# litellm/types/router.py) that cooldown_list is really just a flat list of
# deployment-ID strings with no embedded cooldown_time per entry -- there is
# no supported way to introspect "how long is provider X's cooldown" from
# outside the Router without reaching into private cache internals that can
# change between litellm versions. So we don't try to be clever about WHICH
# provider is stuck; instead we bound the total wall-clock time we're willing
# to wait for the whole call (across all of Router's internal retries and
# fallbacks combined), in a background thread we can walk away from if it
# blows the budget.
#
# This does NOT cancel the Router's own background attempt (Python can't
# forcibly kill a thread that's blocked in a C-level socket read), so the
# call keeps running in memory even after we time out and raise back to the
# agent loop -- but the agent stops WAITING on it, self-reports honestly that
# it ran out of patience, and the caller can retry or bail. That's a small,
# deliberately conservative fix: not "smarter routing", just "never block
# silently past a hard, visible ceiling again".
# ---------------------------------------------------------------------------

DEFAULT_CHAT_TIMEOUT_SECONDS = 90  # generous for a single multi-provider round trip


class LLMTimeoutError(TimeoutError):
    """Raised when chat_completion() exceeds its overall wall-clock budget.
    Distinct from litellm's own exceptions so callers can catch it
    specifically without accidentally swallowing real provider errors."""
    pass


def _run_with_deadline(fn, timeout_seconds: float, *args, **kwargs):
    """Run fn(*args, **kwargs) in a background thread and enforce a real
    wall-clock deadline on waiting for it, regardless of what's happening
    inside litellm's own retry/backoff/fallback machinery.

    NOTE: this does not kill the background thread on timeout (Python has no
    safe way to do that) -- it just stops the caller from waiting past the
    deadline and raises LLMTimeoutError instead. Confirmed via direct test
    (see test/llm_timeout_test.py) that a slow call is interrupted at the
    deadline rather than the full duration.
    """
    result_q: "queue.Queue" = queue.Queue(maxsize=1)

    def _runner():
        try:
            result_q.put(("ok", fn(*args, **kwargs)))
        except Exception as e:  # noqa: BLE001 - forward any exception, don't swallow
            result_q.put(("error", e))

    t = threading.Thread(target=_runner, daemon=True)
    started = time.monotonic()
    t.start()
    try:
        status, value = result_q.get(timeout=timeout_seconds)
    except queue.Empty:
        elapsed = time.monotonic() - started
        raise LLMTimeoutError(
            f"LLM call exceeded the {timeout_seconds}s wall-clock budget "
            f"(waited {elapsed:.1f}s) -- likely a provider stuck in a long "
            "rate-limit cooldown. The request may still be running in the "
            "background but this call is giving up waiting on it."
        )
    if status == "error":
        raise value
    return value


def chat_completion(
    messages: list,
    tools: Optional[list] = None,
    timeout_seconds: Optional[float] = DEFAULT_CHAT_TIMEOUT_SECONDS,
    **kwargs,
) -> Any:
    """
    Multi-turn, tool-calling-aware completion used by the agent's ReAct loop
    (agent.py). Routes across every configured provider — Groq, Gemini,
    Cerebras, OpenRouter — with automatic retry/failover baked in, where
    "failover" is guaranteed to reach a genuinely different provider once
    retries on the current one are exhausted (see _build_router).

    Enforces an overall wall-clock deadline (default 90s, override with
    timeout_seconds=None to disable) so a provider stuck in a long
    rate-limit cooldown can no longer silently block the caller for tens of
    minutes -- raises LLMTimeoutError instead of hanging. See
    _run_with_deadline for why this is a wrapper around the whole call
    rather than an attempt to introspect Router's internal cooldown state.

    Returns the raw OpenAI-compatible response object, so callers can keep
    doing response.choices[0].message.tool_calls exactly as before; only
    the transport underneath changed.
    """
    router, first_model = get_router()
    call_kwargs = dict(kwargs)
    if tools:
        call_kwargs["tools"] = tools
        call_kwargs["tool_choice"] = "auto"

    if timeout_seconds is None:
        return router.completion(model=first_model, messages=messages, **call_kwargs)

    return _run_with_deadline(
        router.completion,
        timeout_seconds,
        model=first_model,
        messages=messages,
        **call_kwargs,
    )


if __name__ == "__main__":
    # Quick manual smoke test: `python llm_client.py`
    print(ask_llm("Write a Python function to reverse a string"))
