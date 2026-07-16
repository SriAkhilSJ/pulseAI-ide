"""
Direct test of apply_edit/diff_for_edit (tools.py) and the confirmation
gate + cache-invalidation wiring (agent.py / cache.py).

Covers real bugs found and fixed BEFORE this shipped, against an earlier
proposed design:
  1. diff_for_edit() must refuse to preview a sensitive path (the proposal
     skipped this check entirely).
  2. apply_edit() must refuse to write a sensitive path outright.
  3. apply_edit must be added to cache.MUTATING_TOOLS -- otherwise a
     cached read_file() result for a file edited via apply_edit would be
     served as stale/current (the proposal never touched cache.py at all).
  4. agent._needs_confirmation must handle apply_edit via its own
     diff_for_edit-based branch (not silently falling through untouched,
     which would mean apply_edit could write without ever being confirmed).

Run with: PYTHONPATH=/home/user/my-agent python3 test/apply_edit_test.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
import agent  # noqa: E402
from cache import ToolCache, MUTATING_TOOLS  # noqa: E402

SCRATCH = "test/scratch_apply_edit"


def _reset():
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH)


SAMPLE = """def calculate_total(amount, tax):
    subtotal = amount
    total = subtotal + tax
    return total

def format_currency(value):
    return "$" + str(value)
"""


def test_basic_unique_replacement():
    _reset()
    path = os.path.join(SCRATCH, "calc.py")
    with open(path, "w") as f:
        f.write(SAMPLE)

    result = tools.apply_edit(
        path,
        "    total = subtotal + tax\n    return total",
        "    total = subtotal + tax\n    total = total * 1.05\n    return total",
    )
    print("apply_edit result:", result)
    assert result.startswith("OK:"), f"expected success, got: {result}"

    new_content = open(path).read()
    assert "total = total * 1.05" in new_content
    assert "def format_currency" in new_content, "the OTHER function must be untouched"
    print("PASS: unique replacement succeeds and leaves the rest of the file untouched")


def test_missing_old_string_fails_closed():
    _reset()
    path = os.path.join(SCRATCH, "calc.py")
    with open(path, "w") as f:
        f.write(SAMPLE)

    result = tools.apply_edit(path, "this text does not exist anywhere", "replacement")
    print(result)
    assert result.startswith("ERROR:") and "not found" in result
    assert open(path).read() == SAMPLE, "file must be completely unchanged on failure"
    print("PASS: missing old_string fails closed, file untouched")


def test_ambiguous_old_string_fails_closed_with_context():
    _reset()
    path = os.path.join(SCRATCH, "dup.py")
    content = "x = 1\nreturn total\nx = 2\nreturn total\n"
    with open(path, "w") as f:
        f.write(content)

    result = tools.apply_edit(path, "return total", "return total * 2")
    print(result)
    assert result.startswith("ERROR:") and "appears 2 times" in result
    assert "Match 1" in result and "Match 2" in result
    assert open(path).read() == content, "file must be completely unchanged on failure"
    print("PASS: ambiguous old_string fails closed and shows disambiguating context")


def test_sensitive_path_refused_outright():
    result = tools.apply_edit(".env", "FAKE", "FAKE2")
    print(result)
    assert result.startswith("ERROR:") and "sensitive" in result.lower()
    print("PASS: apply_edit refuses a sensitive path outright, same as write_file")


def test_diff_for_edit_refuses_sensitive_path():
    """
    Real bug found in an earlier proposed design: diff_for_edit had no
    is_sensitive_path check at all, meaning the CONFIRMATION PREVIEW path
    could leak a sensitive file's diff even though the actual write was
    separately blocked. Confirm the preview path is equally guarded.
    """
    result = tools.diff_for_edit(".env", "FAKE", "FAKE2")
    assert result is None, "diff_for_edit must never preview a sensitive path's diff"
    print("PASS: diff_for_edit refuses to preview a sensitive path (matches apply_edit's own refusal)")


def test_diff_for_edit_matches_real_change():
    _reset()
    path = os.path.join(SCRATCH, "calc.py")
    with open(path, "w") as f:
        f.write(SAMPLE)

    diff = tools.diff_for_edit(
        path,
        "    total = subtotal + tax\n    return total",
        "    total = subtotal + tax\n    total = total * 1.05\n    return total",
    )
    print(diff)
    assert diff is not None
    assert "total * 1.05" in diff
    print("PASS: diff_for_edit produces a real, accurate preview diff")


def test_confirmation_gate_triggers_for_apply_edit():
    """
    Real bug class this guards against: _needs_confirmation must have an
    explicit branch for apply_edit. If apply_edit were left out entirely
    (as in an earlier draft), it would silently fall through to `return
    None` -- meaning apply_edit calls would NEVER be confirmed, bypassing
    the same safety net write_file gets.
    """
    _reset()
    path = os.path.join(SCRATCH, "calc.py")
    with open(path, "w") as f:
        f.write(SAMPLE)

    confirmation = agent._needs_confirmation(
        "apply_edit",
        {
            "path": path,
            "old_string": "    total = subtotal + tax\n    return total",
            "new_string": "    total = subtotal + tax\n    total = total * 1.05\n    return total",
        },
    )
    assert confirmation is not None, "apply_edit on an existing file with a real change MUST require confirmation"
    reason, diff = confirmation
    print("reason:", reason)
    assert diff is not None and "total * 1.05" in diff
    print("PASS: agent._needs_confirmation correctly gates apply_edit, with a real diff")


def test_apply_edit_is_in_mutating_tools():
    """
    Real bug found and fixed: cache.MUTATING_TOOLS was hardcoded to
    {"write_file", "run_command"} and did NOT include apply_edit when it
    was first added as a new tool -- meaning a cached read_file() result
    would keep being served as current after apply_edit silently changed
    the file on disk.
    """
    assert "apply_edit" in MUTATING_TOOLS, "apply_edit must invalidate the ToolCache, same as write_file"
    print("PASS: apply_edit is registered in cache.MUTATING_TOOLS")


def test_cache_actually_invalidates_after_apply_edit():
    """The real, end-to-end proof -- not just checking set membership."""
    _reset()
    path = os.path.join(SCRATCH, "calc.py")
    with open(path, "w") as f:
        f.write(SAMPLE)

    cache = ToolCache()

    def read_call():
        return tools.read_file(path)

    result1, cached1 = cache.get_or_call("read_file", {"path": path}, read_call)
    assert not cached1
    assert "total = subtotal + tax" in result1
    assert "total * 1.05" not in result1

    # Same read again -- should be served from cache.
    result2, cached2 = cache.get_or_call("read_file", {"path": path}, read_call)
    assert cached2, "second identical read_file call should be served from cache"

    # Now apply_edit through the cache -- this must invalidate everything.
    def edit_call():
        return tools.apply_edit(
            path,
            "    total = subtotal + tax\n    return total",
            "    total = subtotal + tax\n    total = total * 1.05\n    return total",
        )

    edit_result, _ = cache.get_or_call("apply_edit", {
        "path": path, "old_string": "x", "new_string": "y",
    }, edit_call)
    assert edit_result.startswith("OK:")

    # A subsequent read_file call for the SAME path/args must NOT be
    # served from the (now-stale) cache -- it must re-read from disk and
    # see the edit.
    result3, cached3 = cache.get_or_call("read_file", {"path": path}, read_call)
    assert not cached3, "cache must have been invalidated by the apply_edit call"
    assert "total * 1.05" in result3, "the re-read must reflect apply_edit's real on-disk change"
    print("PASS: cache is genuinely invalidated after apply_edit, confirmed via a real stale-vs-fresh read")


if __name__ == "__main__":
    test_basic_unique_replacement()
    test_missing_old_string_fails_closed()
    test_ambiguous_old_string_fails_closed_with_context()
    test_sensitive_path_refused_outright()
    test_diff_for_edit_refuses_sensitive_path()
    test_diff_for_edit_matches_real_change()
    test_confirmation_gate_triggers_for_apply_edit()
    test_apply_edit_is_in_mutating_tools()
    test_cache_actually_invalidates_after_apply_edit()
    _reset()
    shutil.rmtree(SCRATCH)
    print("\nALL TESTS PASSED")
