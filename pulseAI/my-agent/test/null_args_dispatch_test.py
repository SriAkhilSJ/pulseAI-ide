"""
Direct test of a real bug found live while testing git_tools integration:
a tool call with the literal JSON string "null" as its arguments (which
some providers emit for zero-arg tool calls, instead of "{}") crashed
_dispatch_tool_call with `TypeError: ... argument after ** must be a
mapping, not NoneType` because json.loads("null") returns None, and
`args or "{}"` doesn't catch it (the non-empty string "null" already
satisfied the `or`, so json.loads received "null", not "{}").

Run with: PYTHONPATH=/home/user/my-agent python3 test/null_args_dispatch_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


def test_null_arguments_treated_as_empty():
    # list_files has an optional `directory` param with a default -- a
    # realistic zero/optional-arg tool, same shape as the new git_status.
    result = agent._dispatch_tool_call("list_files", "null")
    assert not result.startswith("ERROR: bad arguments"), f"null args should not crash the tool: {result}"
    print("PASS: 'null' arguments string is treated as {} instead of crashing:", result[:80])


def test_empty_string_arguments_still_works():
    result = agent._dispatch_tool_call("list_files", "")
    assert not result.startswith("ERROR: bad arguments"), f"empty args should not crash: {result}"
    print("PASS: empty-string arguments still work as before")


def test_real_object_arguments_still_work():
    result = agent._dispatch_tool_call("list_files", '{"directory": "."}')
    assert not result.startswith("ERROR"), f"real args should work: {result}"
    print("PASS: normal object arguments still work")


if __name__ == "__main__":
    test_null_arguments_treated_as_empty()
    test_empty_string_arguments_still_works()
    test_real_object_arguments_still_work()
    print("\nALL TESTS PASSED")
