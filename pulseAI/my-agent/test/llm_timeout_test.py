"""
Direct test of llm_client's overall wall-clock deadline (Gap 1 fix).

Simulates the real production failure: Router.completion() blocked for a
long time (standing in for the 2185s Groq cooldown hang actually observed
and diagnosed with py-spy during the 3-mission stress test). Confirms
chat_completion() raises LLMTimeoutError at the configured deadline instead
of hanging for the full duration.

Run with: PYTHONPATH=/home/user/my-agent python3 test/llm_timeout_test.py
"""
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client  # noqa: E402


def fake_slow_completion(*args, **kwargs):
    # Simulate the real observed hang: Router.completion() blocked far
    # longer than any reasonable caller should wait (the actual incident
    # was 2185s / 36+ minutes -- we use 5s here so the test runs fast, but
    # the mechanism under test is identical).
    time.sleep(5)
    return "SHOULD_NEVER_BE_SEEN_BY_CALLER"


def test_timeout_fires_before_full_duration():
    router, first_model = llm_client.get_router()
    with patch.object(router, "completion", side_effect=fake_slow_completion):
        start = time.monotonic()
        try:
            llm_client.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                timeout_seconds=1,
            )
            print("FAIL: expected LLMTimeoutError, got a normal return")
            sys.exit(1)
        except llm_client.LLMTimeoutError as e:
            elapsed = time.monotonic() - start
            print(f"caught LLMTimeoutError after {elapsed:.2f}s: {e}")
            assert elapsed < 3, f"took too long to raise: {elapsed:.2f}s (should be ~1s)"
            print("PASS: raised well before the simulated 5s hang completed")


def test_normal_call_still_works_under_deadline():
    router, first_model = llm_client.get_router()

    def fake_fast_completion(*args, **kwargs):
        return "fast response"

    with patch.object(router, "completion", side_effect=fake_fast_completion):
        result = llm_client.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            timeout_seconds=5,
        )
        assert result == "fast response"
        print("PASS: fast call under the deadline returns normally")


def test_timeout_seconds_none_disables_deadline():
    router, first_model = llm_client.get_router()

    def fake_fast_completion(*args, **kwargs):
        return "no-deadline response"

    with patch.object(router, "completion", side_effect=fake_fast_completion):
        result = llm_client.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            timeout_seconds=None,
        )
        assert result == "no-deadline response"
        print("PASS: timeout_seconds=None bypasses the deadline wrapper entirely")


if __name__ == "__main__":
    test_timeout_fires_before_full_duration()
    test_normal_call_still_works_under_deadline()
    test_timeout_seconds_none_disables_deadline()
    print("\nALL TESTS PASSED")
