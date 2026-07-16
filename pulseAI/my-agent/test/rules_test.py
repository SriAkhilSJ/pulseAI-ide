"""
Direct tests for rules.py, WITHOUT calling any real LLM (isolates parsing/
scanning/matching correctness from LLM non-determinism -- same philosophy
as test/skills_test.py). A separate live test (test/rules_live_test.py)
exercises the mid-task injection through a real run_agent() call with a
real LLM.

Run with: PYTHONPATH=/home/user/my-agent python3 test/rules_test.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402 -- import first, same reason as skills_test.py
import rules  # noqa: E402

SCRATCH_RULES_DIR = Path("test/scratch/rules_test_dir")


def _reset():
    if SCRATCH_RULES_DIR.exists():
        shutil.rmtree(SCRATCH_RULES_DIR)
    SCRATCH_RULES_DIR.mkdir(parents=True)


def _write_rule(name: str, content: str) -> Path:
    path = SCRATCH_RULES_DIR / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_rule_text -- the real "frontmatter is optional" difference from skills
# ---------------------------------------------------------------------------

def test_parse_plain_markdown_no_frontmatter_at_all():
    """The real, deliberate difference from skills.py: a rule with ZERO
    YAML frontmatter is VALID (matching a real .cursorrules/CLAUDE.md,
    which have no required frontmatter at all) -- the whole file is the
    body, always-loaded."""
    text = "# Testing Conventions\n\nUse pytest, not unittest.\nMock all external APIs.\n"
    rule = rules.parse_rule_text(text, name="testing", source_path=Path("testing.md"))
    assert rule.paths_glob is None
    assert "Use pytest" in rule.body
    print("PASS: a rule with no frontmatter at all is valid, always-loaded, whole file is the body")


def test_parse_frontmatter_with_paths_field():
    text = "---\npaths: src/api/**/*.ts\n---\nAll API endpoints must validate input.\n"
    rule = rules.parse_rule_text(text, name="api-rules", source_path=Path("api-rules.md"))
    assert rule.paths_glob == "src/api/**/*.ts"
    assert "validate input" in rule.body
    print("PASS: a rule with 'paths:' frontmatter is correctly parsed as path-scoped")


def test_parse_frontmatter_with_globs_field_cursor_style():
    """Cursor's own field name is 'globs:', not 'paths:' -- both should work."""
    text = "---\nglobs: \"*.tsx,*.ts\"\n---\nUse TypeScript strict mode.\n"
    rule = rules.parse_rule_text(text, name="ts-rules", source_path=Path("ts-rules.md"))
    assert rule.paths_glob == "*.tsx,*.ts"
    print("PASS: Cursor's 'globs:' field name is also accepted (not just Claude Code's 'paths:')")


def test_parse_frontmatter_paths_as_yaml_list():
    text = "---\npaths:\n  - src/api/**/*.ts\n  - src/models/**/*.ts\n---\nBackend conventions.\n"
    rule = rules.parse_rule_text(text, name="backend", source_path=Path("backend.md"))
    assert "src/api/**/*.ts" in rule.paths_glob
    assert "src/models/**/*.ts" in rule.paths_glob
    print("PASS: a YAML list of paths is normalized into a single joinable pattern string")


def test_parse_malformed_frontmatter_raises_clear_error():
    """Unlike 'no frontmatter at all' (valid), a file that STARTS with ---
    but is genuinely malformed must still raise a clear error -- reuses
    skills.py's parse_frontmatter, so this is really testing the
    integration, not re-testing skills.py's own already-tested logic."""
    text = "---\npaths: [unterminated\n---\nbody\n"
    try:
        rules.parse_rule_text(text, name="broken", source_path=Path("broken.md"))
        assert False, "expected a ValueError for malformed frontmatter"
    except ValueError as e:
        assert "yaml" in str(e).lower()
        print(f"PASS: malformed frontmatter (not 'no frontmatter') still raises a clear ValueError: {e}")


def test_parse_frontmatter_present_but_no_paths_field():
    """A rule CAN have frontmatter for other reasons (future-proofing) but
    no paths: field at all -- still always-loaded, not an error."""
    text = "---\nauthor: someone\n---\nGeneral guidance.\n"
    rule = rules.parse_rule_text(text, name="general", source_path=Path("general.md"))
    assert rule.paths_glob is None
    print("PASS: frontmatter present but with no paths/globs field is still valid and always-loaded")


# ---------------------------------------------------------------------------
# rule_matches_path -- THE real stdlib bug this module was built around
# ---------------------------------------------------------------------------

def test_glob_matching_direct_child_the_real_pathlib_bug():
    """THE real bug found and fixed before this module shipped:
    pathlib.PurePosixPath.match() does NOT correctly handle '**' matching
    a file DIRECTLY inside the scoped directory (only nested files) --
    confirmed directly this session. glob.translate handles it correctly.
    This is the single most important test in this file."""
    rule = rules.Rule(name="api", body="body", source_path=Path("x"), paths_glob="src/api/**/*.ts")
    assert rules.rule_matches_path(rule, "src/api/foo.ts") is True, (
        "REGRESSION: a file DIRECTLY inside the scoped directory must match "
        "'**' (globstar matches ZERO or more directories) -- this is the exact "
        "stdlib pathlib.match() bug this module was built to avoid"
    )
    print("PASS: 'src/api/**/*.ts' correctly matches a file DIRECTLY in src/api/ (the real pathlib bug this avoids)")


def test_glob_matching_nested_child():
    rule = rules.Rule(name="api", body="body", source_path=Path("x"), paths_glob="src/api/**/*.ts")
    assert rules.rule_matches_path(rule, "src/api/nested/foo.ts") is True
    assert rules.rule_matches_path(rule, "src/api/deep/nested/foo.ts") is True
    print("PASS: '**' correctly matches files nested arbitrarily deep")


def test_glob_matching_non_matching_path():
    rule = rules.Rule(name="api", body="body", source_path=Path("x"), paths_glob="src/api/**/*.ts")
    assert rules.rule_matches_path(rule, "src/other/foo.ts") is False
    assert rules.rule_matches_path(rule, "src/api/foo.py") is False  # wrong extension
    print("PASS: a genuinely non-matching path correctly does not match")


def test_glob_matching_simple_extension_pattern():
    rule = rules.Rule(name="ts", body="body", source_path=Path("x"), paths_glob="*.ts")
    assert rules.rule_matches_path(rule, "foo.ts") is True
    assert rules.rule_matches_path(rule, "src/foo.ts") is False  # bare *.ts doesn't cross directories
    print("PASS: a simple '*.ts' pattern matches only top-level .ts files, not nested ones (no implicit **)")


def test_glob_matching_multiple_patterns_joined():
    rule = rules.Rule(name="multi", body="body", source_path=Path("x"), paths_glob="*.ts|*.tsx")
    assert rules.rule_matches_path(rule, "foo.ts") is True
    assert rules.rule_matches_path(rule, "foo.tsx") is True
    assert rules.rule_matches_path(rule, "foo.py") is False
    print("PASS: multiple glob patterns (joined with '|' from a YAML list) match via alternation")


def test_glob_matching_always_loaded_rule_never_matches():
    """A rule with NO paths_glob (always-loaded) must never 'match' via
    this function -- it's already in the system prompt from the start,
    there's nothing to trigger."""
    rule = rules.Rule(name="general", body="body", source_path=Path("x"), paths_glob=None)
    assert rules.rule_matches_path(rule, "anything.py") is False
    print("PASS: an always-loaded rule (no paths_glob) never matches via rule_matches_path")


def test_glob_matching_malformed_pattern_fails_safe():
    """A malformed glob pattern in a rule's frontmatter must not crash the
    whole dispatch loop -- fails safe (never matches), not an exception."""
    rule = rules.Rule(name="broken", body="body", source_path=Path("x"), paths_glob="[unterminated")
    result = rules.rule_matches_path(rule, "anything.py")
    assert result is False
    print("PASS: a malformed glob pattern fails safe (returns False) instead of crashing")


# ---------------------------------------------------------------------------
# scan_rules -- per-rule isolation (same real bug class as skills.py)
# ---------------------------------------------------------------------------

def test_scan_rules_empty_directory():
    _reset()
    result = rules.scan_rules(rules_dir=SCRATCH_RULES_DIR)
    assert result == {}
    print("PASS: scanning an empty rules directory returns an empty dict, no crash")


def test_scan_rules_one_malformed_rule_does_not_take_down_others():
    _reset()
    _write_rule("good-one", "# Good rule one\nAlways use TypeScript.")
    _write_rule("broken", "---\npaths: [unterminated\n---\nbody")
    _write_rule("good-two", "---\npaths: src/api/**/*.ts\n---\nAPI conventions.")

    result = rules.scan_rules(rules_dir=SCRATCH_RULES_DIR)
    assert result["good-one"][0] is not None
    assert result["good-two"][0] is not None
    assert result["broken"][0] is None
    assert result["broken"][1] is not None
    print(f"PASS: one malformed rule (error: {result['broken'][1][:50]}...) does not prevent "
          f"the other 2 valid rules from loading")


def test_scan_rules_recursive_subdirectories():
    """Real, verified behavior claim: Claude Code's own docs say rules are
    'discovered recursively' including subdirectories -- confirmed this
    module's scan_rules uses rglob, not glob, matching that."""
    _reset()
    (SCRATCH_RULES_DIR / "backend").mkdir()
    (SCRATCH_RULES_DIR / "backend" / "api.md").write_text("Backend API rules.", encoding="utf-8")
    _write_rule("general", "General rules.")

    result = rules.scan_rules(rules_dir=SCRATCH_RULES_DIR)
    assert "api" in result and result["api"][0] is not None
    assert "general" in result and result["general"][0] is not None
    print("PASS: rules are discovered recursively in subdirectories, matching Claude Code's real documented behavior")


# ---------------------------------------------------------------------------
# get_always_loaded_block / get_path_scoped_rules
# ---------------------------------------------------------------------------

def test_always_loaded_block_excludes_path_scoped_rules():
    _reset()
    _write_rule("general", "Always use TypeScript.")
    _write_rule("api-only", "---\npaths: src/api/**/*.ts\n---\nAPI-specific rule.")

    scanned = rules.scan_rules(rules_dir=SCRATCH_RULES_DIR)
    block = rules.get_always_loaded_block(scanned)
    assert "Always use TypeScript" in block
    assert "API-specific rule" not in block, "a path-scoped rule must NOT appear in the always-loaded block"
    print("PASS: get_always_loaded_block includes always-loaded rules but excludes path-scoped ones")


def test_path_scoped_rules_extraction():
    _reset()
    _write_rule("general", "Always use TypeScript.")
    _write_rule("api-only", "---\npaths: src/api/**/*.ts\n---\nAPI-specific rule.")

    scanned = rules.scan_rules(rules_dir=SCRATCH_RULES_DIR)
    path_scoped = rules.get_path_scoped_rules(scanned)
    assert len(path_scoped) == 1
    assert path_scoped[0].name == "api-only"
    print("PASS: get_path_scoped_rules correctly extracts only the path-scoped rule")


def test_always_loaded_block_includes_root_agents_md():
    """AGENTS.md at the real project root (tools.WORKDIR) -- uses the
    REAL root, not the scratch dir, since _load_root_rule_file always
    looks at tools.WORKDIR (matching how it will actually be used)."""
    real_workdir = tools.WORKDIR
    agents_md_path = real_workdir / "AGENTS.md"
    already_existed = agents_md_path.exists()
    original_content = agents_md_path.read_text(encoding="utf-8") if already_existed else None
    try:
        agents_md_path.write_text("# Test Project\nUnique marker: XYZZY123.\n", encoding="utf-8")
        block = rules.get_always_loaded_block({})
        assert "XYZZY123" in block
        print("PASS: a real AGENTS.md at the project root is included in the always-loaded block")
    finally:
        if already_existed:
            agents_md_path.write_text(original_content, encoding="utf-8")
        else:
            agents_md_path.unlink()


# ---------------------------------------------------------------------------
# Tool wrapper + registration
# ---------------------------------------------------------------------------

def test_rules_registered_in_tools_registry():
    assert tools.RULES_AVAILABLE is True
    assert "list_rules" in tools.TOOL_FUNCTIONS
    spec_names = {s["function"]["name"] for s in tools.TOOL_SPECS}
    assert "list_rules" in spec_names
    print("PASS: list_rules is registered in tools.TOOL_FUNCTIONS/TOOL_SPECS")


def test_list_rules_tool_shows_errors_for_broken_rules():
    real_rules_dir = rules._rules_dir()
    test_rule_dir_file = real_rules_dir / "_test_broken_rule_temp.md"
    real_rules_dir.mkdir(parents=True, exist_ok=True)
    test_rule_dir_file.write_text("---\npaths: [unterminated\n---\nbody", encoding="utf-8")
    try:
        result = rules._tool_list_rules()
        assert "ERROR" in result
        assert "_test_broken_rule_temp" in result
        print("PASS: list_rules tool shows a clear ERROR entry for a broken rule file")
    finally:
        test_rule_dir_file.unlink()


def test_tool_spec_schema_matches_documented_parameters():
    spec = next(s for s in rules.TOOL_SPECS if s["function"]["name"] == "list_rules")
    assert spec["function"]["parameters"]["properties"] == {}
    print("PASS: list_rules schema exposes no parameters, as documented")


if __name__ == "__main__":
    test_parse_plain_markdown_no_frontmatter_at_all()
    test_parse_frontmatter_with_paths_field()
    test_parse_frontmatter_with_globs_field_cursor_style()
    test_parse_frontmatter_paths_as_yaml_list()
    test_parse_malformed_frontmatter_raises_clear_error()
    test_parse_frontmatter_present_but_no_paths_field()
    test_glob_matching_direct_child_the_real_pathlib_bug()
    test_glob_matching_nested_child()
    test_glob_matching_non_matching_path()
    test_glob_matching_simple_extension_pattern()
    test_glob_matching_multiple_patterns_joined()
    test_glob_matching_always_loaded_rule_never_matches()
    test_glob_matching_malformed_pattern_fails_safe()
    test_scan_rules_empty_directory()
    test_scan_rules_one_malformed_rule_does_not_take_down_others()
    test_scan_rules_recursive_subdirectories()
    test_always_loaded_block_excludes_path_scoped_rules()
    test_path_scoped_rules_extraction()
    test_always_loaded_block_includes_root_agents_md()
    test_rules_registered_in_tools_registry()
    test_list_rules_tool_shows_errors_for_broken_rules()
    test_tool_spec_schema_matches_documented_parameters()
    _reset()
    if SCRATCH_RULES_DIR.exists():
        shutil.rmtree(SCRATCH_RULES_DIR)
    print("\nALL TESTS PASSED")
