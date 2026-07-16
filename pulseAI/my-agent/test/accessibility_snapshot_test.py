"""
Direct test of get_accessibility_snapshot (tools_browser.py), run against
the REAL test/finance_dashboard/index.html (not synthetic data) and a real
live URL.

Real bug this replaces: a proposed implementation used
`page.accessibility.snapshot()`, which does NOT EXIST in the actually
installed Playwright (1.61.0) -- confirmed directly:
`AttributeError: 'Page' object has no attribute 'accessibility'`. That
whole API was removed from Playwright years ago. The real, current
replacement is `Locator.aria_snapshot(depth=...)`.

Run with: PYTHONPATH=/home/user/my-agent python3 test/accessibility_snapshot_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


def test_local_file_snapshot():
    if not tools.BROWSER_TOOLS_AVAILABLE:
        print("SKIP: browser tools not available")
        return
    result = tools.TOOL_FUNCTIONS["get_accessibility_snapshot"](
        file_path="test/finance_dashboard/index.html", depth=6
    )
    print(result)
    assert "ERROR" not in result.split("\n")[0], f"should succeed, got: {result[:200]}"
    assert 'heading "Total Balance"' in result, "should find the real Total Balance heading"
    assert 'heading "Recent Transactions"' in result, "should find the real Recent Transactions heading"
    assert "columnheader" in result, "should find real table column headers"
    print("PASS: local file accessibility snapshot matches real page content")


def test_mutual_exclusivity_guard():
    r1 = tools.TOOL_FUNCTIONS["get_accessibility_snapshot"]()
    assert "ERROR" in r1 and "exactly one" in r1
    r2 = tools.TOOL_FUNCTIONS["get_accessibility_snapshot"](
        file_path="test/finance_dashboard/index.html", url="https://example.com"
    )
    assert "ERROR" in r2 and "exactly one" in r2
    print("PASS: rejects both file_path+url and neither")


def test_sensitive_path_blocked():
    result = tools.TOOL_FUNCTIONS["get_accessibility_snapshot"](file_path=".env")
    assert "ERROR" in result and "sensitive" in result.lower()
    print("PASS: .env is refused, same as every other file-reading tool")


def test_live_url_snapshot():
    if not tools.BROWSER_TOOLS_AVAILABLE:
        print("SKIP: browser tools not available")
        return
    result = tools.TOOL_FUNCTIONS["get_accessibility_snapshot"](url="https://example.com", depth=3)
    print(result)
    assert "ERROR" not in result.split("\n")[0]
    assert "Example Domain" in result
    print("PASS: live URL accessibility snapshot works")


def test_no_orphaned_browser_or_server_processes():
    import subprocess
    tools.TOOL_FUNCTIONS["get_accessibility_snapshot"](
        file_path="test/finance_dashboard/index.html", depth=4
    )
    ps_out = subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout
    leaked = [line for line in ps_out.splitlines() if ("chrome" in line.lower() or "chromium" in line.lower()) and "grep" not in line]
    assert not leaked, f"browser process(es) leaked after the call: {leaked}"
    print("PASS: no leaked browser processes after the call completes")


if __name__ == "__main__":
    test_mutual_exclusivity_guard()
    test_sensitive_path_blocked()
    test_local_file_snapshot()
    test_live_url_snapshot()
    test_no_orphaned_browser_or_server_processes()
    print("\nALL TESTS PASSED")
