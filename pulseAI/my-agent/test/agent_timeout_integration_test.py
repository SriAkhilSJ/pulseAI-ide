"""
Integration test: confirms agent.run_agent() surfaces LLMTimeoutError as a
graceful final_reply instead of hanging or crashing with a stack trace.

Run with: PYTHONPATH=/home/user/my-agent python3 test/agent_timeout_integration_test.py
"""
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import llm_client  # noqa: E402


def fake_timeout(*args, **kwargs):
    raise llm_client.LLMTimeoutError("simulated: every provider stuck in cooldown")


def main():
    with patch.object(llm_client, "chat_completion", side_effect=fake_timeout):
        start = time.monotonic()
        reply = agent.run_agent("say hello", verbose=False, log=lambda *a: None)
        elapsed = time.monotonic() - start
        print(f"run_agent returned in {elapsed:.2f}s: {reply!r}")
        assert "timeout" in reply.lower() or "slow" in reply.lower() or "rate-limited" in reply.lower(), \
            f"expected a graceful timeout message, got: {reply}"
        assert elapsed < 5, f"should return almost immediately once the mock raises, took {elapsed:.2f}s"
        print("PASS: agent surfaces LLMTimeoutError as a graceful reply, no hang, no crash")


if __name__ == "__main__":
    main()
