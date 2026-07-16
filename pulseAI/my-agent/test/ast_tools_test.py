"""
Direct test of ast_tools.py, including ACTUALLY RUNNING the transformed
JavaScript via node to prove correctness (not just string-matching), per
this project's "actually execute code, don't just trust it looks right"
standard.

Run with: PYTHONPATH=/home/user/my-agent python3 test/ast_tools_test.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ast_tools  # noqa: E402

SAMPLE_JS = """// Test file for AST transforms
var globalCounter = 0;   // mutated via ++ below -- must stay var
var config = { debug: true };  // never mutated -- safe to become const
var apiKey = "secret";   // never mutated -- safe to become const

function calculateTotal(amount, tax) {
    var subtotal = amount;      // never mutated -- safe to become const
    var total = subtotal + tax; // mutated via += below -- must stay var
    total += tax * 0.05;
    return total;
}

function formatCurrency(value) {
    return "$" + value.toFixed(2);
}

globalCounter++;
"""


def _run_node(js_source: str) -> tuple[int, str]:
    """Actually execute JS via node and return (exit_code, combined_output)."""
    path = "/tmp/ast_tools_test_sample.js"
    with open(path, "w") as f:
        f.write(js_source)
    result = subprocess.run(["node", path], capture_output=True, text=True, timeout=10)
    return result.returncode, (result.stdout + result.stderr)


def test_original_sample_runs_cleanly():
    """Sanity check: the ORIGINAL (untransformed) sample must itself run
    without error, so any later failure is attributable to the transform,
    not a broken test fixture."""
    exit_code, output = _run_node(SAMPLE_JS)
    assert exit_code == 0, f"original sample should run cleanly, got exit {exit_code}: {output}"
    print("PASS: original sample JS runs cleanly (sanity check)")


def test_augmented_assignment_and_increment_correctly_block_conversion():
    """
    THE critical safety bug this module was built to fix (found via direct
    API testing before writing any transform code): a variable mutated via
    `+=` (augmented_assignment_expression) or `++`/`--` (update_expression)
    -- NOT just plain `=` (assignment_expression) -- must NOT be converted
    to const, or the resulting code throws
    `TypeError: Assignment to constant variable` at runtime.
    """
    result = ast_tools.transform_var_to_const_safe(SAMPLE_JS)
    print("--- transformed source ---")
    print(result)
    print("--- end transformed source ---")

    assert "var globalCounter" in result, "globalCounter is mutated via ++ -- MUST stay var"
    assert "var total" in result, "total is mutated via += -- MUST stay var"
    assert "const config" in result, "config is never mutated -- should become const"
    assert "const apiKey" in result, "apiKey is never mutated -- should become const"
    assert "const subtotal" in result, "subtotal is never mutated -- should become const"
    print("PASS: correct var/const split based on ALL mutation types (=, +=, ++)")


def test_transformed_code_actually_runs_without_crashing():
    """
    The real proof: actually EXECUTE the transformed JS via node. If the
    augmented-assignment/increment bug were still present, this would
    throw `TypeError: Assignment to constant variable` -- a string-based
    assertion alone (checking substrings) can't catch a RUNTIME error like
    this; only actually running the code can.
    """
    result = ast_tools.transform_var_to_const_safe(SAMPLE_JS)
    exit_code, output = _run_node(result)
    assert exit_code == 0, (
        f"transformed code MUST still run without error, got exit {exit_code}:\n{output}"
    )
    assert "Assignment to constant variable" not in output
    print("PASS: transformed code actually executes via node with no runtime error")


def test_no_vars_to_transform_returns_unchanged():
    source = "let x = 1;\nconst y = 2;\n"
    result = ast_tools.transform_var_to_const_safe(source)
    assert result == source, "a file with no `var` at all should be returned unchanged"
    print("PASS: file with no var declarations is returned unchanged")


def test_multi_declarator_all_or_nothing():
    """
    `var a = 1, b = 2;` where `a` is mutated but `b` isn't -- the WHOLE
    statement must stay `var` (JS doesn't support per-declarator const/var
    mixing within one declaration statement).
    """
    source = "var a = 1, b = 2;\na = 3;\n"
    result = ast_tools.transform_var_to_const_safe(source)
    assert "var a = 1, b = 2;" in result, "mixed-safety multi-declarator statement must stay var entirely"
    print("PASS: multi-declarator statement with mixed safety stays var (all-or-nothing)")

    exit_code, output = _run_node(result)
    assert exit_code == 0, f"result must still run: {output}"


def test_add_jsdoc_to_function():
    result = ast_tools.add_jsdoc_to_function(
        SAMPLE_JS, "calculateTotal", {"amount": "number", "tax": "number"}, "number"
    )
    assert "/**" in result
    assert "@param {number} amount" in result
    assert "@param {number} tax" in result
    assert "@returns {number}" in result
    # The JSDoc must appear BEFORE calculateTotal, not before formatCurrency.
    jsdoc_pos = result.index("/**")
    calc_pos = result.index("function calculateTotal")
    format_pos = result.index("function formatCurrency")
    assert jsdoc_pos < calc_pos < format_pos, "JSDoc must be inserted immediately before calculateTotal, not elsewhere"
    print("PASS: JSDoc inserted in the correct location with correct content")

    exit_code, output = _run_node(result)
    assert exit_code == 0, f"code with JSDoc comment must still run: {output}"
    print("PASS: code with inserted JSDoc still executes correctly (comment doesn't break syntax)")


def test_add_jsdoc_function_not_found_raises():
    try:
        ast_tools.add_jsdoc_to_function(SAMPLE_JS, "nonExistentFunction", {}, "void")
        print("FAIL: expected ValueError")
        sys.exit(1)
    except ValueError as e:
        print("PASS: add_jsdoc_to_function raises ValueError for a missing function:", e)


def test_find_untyped_functions():
    documented = "/**\n * Adds two numbers.\n */\nfunction add(a, b) {\n    return a + b;\n}\n\nfunction subtract(a, b) {\n    return a - b;\n}\n"
    result = ast_tools.find_untyped_functions(documented)
    names = {f["name"] for f in result}
    assert "subtract" in names, "subtract has no JSDoc -- should be flagged"
    assert "add" not in names, "add HAS a JSDoc immediately above it -- should NOT be flagged"
    print("PASS: find_untyped_functions correctly distinguishes documented vs undocumented functions")


if __name__ == "__main__":
    if not ast_tools.AST_TOOLS_AVAILABLE:
        print("SKIP: tree-sitter not available")
        sys.exit(0)
    test_original_sample_runs_cleanly()
    test_augmented_assignment_and_increment_correctly_block_conversion()
    test_transformed_code_actually_runs_without_crashing()
    test_no_vars_to_transform_returns_unchanged()
    test_multi_declarator_all_or_nothing()
    test_add_jsdoc_to_function()
    test_add_jsdoc_function_not_found_raises()
    test_find_untyped_functions()
    print("\nALL TESTS PASSED")
