"""
Direct tests for skills.py, WITHOUT calling any real LLM (isolates parsing/
scanning/registration correctness from LLM non-determinism -- same
philosophy as test/subagents_test.py / test/permissions_test.py). A
separate live test (test/skills_live_test.py) exercises this through a
real run_agent() call with a real LLM choosing to call load_skill.

Run with: PYTHONPATH=/home/user/my-agent python3 test/skills_test.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402 -- import first, same reason as subagents_test.py/rag_indexer_test.py
import skills  # noqa: E402

SCRATCH_SKILLS_DIR = Path("test/scratch/skills_test_dir")


def _reset():
    if SCRATCH_SKILLS_DIR.exists():
        shutil.rmtree(SCRATCH_SKILLS_DIR)
    SCRATCH_SKILLS_DIR.mkdir(parents=True)


def _write_skill(name: str, description: str, body: str, extra_frontmatter: str = "") -> Path:
    skill_dir = SCRATCH_SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra_frontmatter}---\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


# ---------------------------------------------------------------------------
# parse_skill_text
# ---------------------------------------------------------------------------

def test_parse_normal_skill():
    text = "---\nname: react-component\ndescription: Build React components\n---\nStep 1: do X\n"
    skill = skills.parse_skill_text(text, root=Path("."))
    assert skill.name == "react-component"
    assert skill.description == "Build React components"
    assert skill.body == "Step 1: do X"
    print("PASS: a normal, well-formed skill parses correctly")


def test_parse_body_containing_literal_horizontal_rule():
    """The real edge case checked directly against the regex before this
    module shipped: a skill body that itself contains '---' as a markdown
    horizontal rule must not be mistaken for a second frontmatter block."""
    text = "---\nname: x\ndescription: y\n---\nStep 1\n\n---\n\nStep 2\n"
    skill = skills.parse_skill_text(text, root=Path("."))
    assert "Step 1" in skill.body and "Step 2" in skill.body and "---" in skill.body
    print("PASS: a '---' horizontal rule INSIDE the body is preserved, not mistaken for frontmatter delimiters")


def test_parse_missing_closing_delimiter_raises_clear_error():
    """The real bug found and fixed before this shipped: a naive
    text.split('---', 2) approach raises an unhandled ValueError on
    unpacking when there's no closing delimiter at all. This must raise a
    CLEAR, catchable ValueError instead -- never an unhandled crash, and
    never a silently wrong parse."""
    text = "---\nname: broken\nno closing delimiter here at all\n"
    try:
        skills.parse_skill_text(text, root=Path("."))
        assert False, "expected a ValueError for a missing closing delimiter"
    except ValueError as e:
        assert "delimiter" in str(e).lower() or "frontmatter" in str(e).lower()
        print(f"PASS: a missing closing delimiter raises a clear ValueError: {e}")


def test_parse_no_frontmatter_at_all_raises_clear_error():
    text = "Just plain markdown, no frontmatter at all"
    try:
        skills.parse_skill_text(text, root=Path("."))
        assert False, "expected a ValueError"
    except ValueError:
        print("PASS: text with no frontmatter at all raises a clear ValueError")


def test_parse_malformed_yaml_raises_clear_error():
    text = "---\nname: test\ndescription: [unterminated bracket\n---\nbody\n"
    try:
        skills.parse_skill_text(text, root=Path("."))
        assert False, "expected a ValueError for malformed YAML"
    except ValueError as e:
        assert "yaml" in str(e).lower()
        print(f"PASS: malformed YAML raises a clear ValueError: {e}")


def test_parse_missing_required_name_raises_clear_error():
    text = "---\ndescription: no name field\n---\nbody\n"
    try:
        skills.parse_skill_text(text, root=Path("."))
        assert False, "expected a ValueError for missing name"
    except ValueError as e:
        assert "name" in str(e).lower()
        print(f"PASS: missing 'name' field raises a clear ValueError: {e}")


def test_parse_missing_required_description_raises_clear_error():
    text = "---\nname: no-description\n---\nbody\n"
    try:
        skills.parse_skill_text(text, root=Path("."))
        assert False, "expected a ValueError for missing description"
    except ValueError as e:
        assert "description" in str(e).lower()
        print(f"PASS: missing 'description' field raises a clear ValueError: {e}")


def test_parse_tools_hint_string_form():
    text = "---\nname: t\ndescription: d\ndisallowed-tools: Bash, Write\n---\nbody\n"
    skill = skills.parse_skill_text(text, root=Path("."))
    assert skill.tools_hint == "Bash, Write"
    print("PASS: disallowed-tools as a plain string is captured as tools_hint")


def test_parse_tools_hint_list_form():
    text = "---\nname: t\ndescription: d\ndisallowed-tools:\n  - Bash\n  - Write\n---\nbody\n"
    skill = skills.parse_skill_text(text, root=Path("."))
    assert skill.tools_hint == "Bash, Write"
    print("PASS: disallowed-tools as a YAML list is normalized to a comma-joined string")


def test_parse_allowed_tools_fallback_when_no_disallowed():
    text = "---\nname: t\ndescription: d\nallowed-tools: Read Grep\n---\nbody\n"
    skill = skills.parse_skill_text(text, root=Path("."))
    assert skill.tools_hint == "Read Grep"
    print("PASS: allowed-tools is captured as tools_hint when disallowed-tools is absent")


# ---------------------------------------------------------------------------
# scan_skills -- the real bug this fixes: per-skill isolation
# ---------------------------------------------------------------------------

def test_scan_skills_empty_directory_returns_empty_dict():
    _reset()
    result = skills.scan_skills(skills_dir=SCRATCH_SKILLS_DIR)
    assert result == {}
    print("PASS: scanning an empty directory returns an empty dict, no crash")


def test_scan_skills_nonexistent_directory_returns_empty_dict():
    result = skills.scan_skills(skills_dir=Path("test/scratch/does_not_exist_at_all"))
    assert result == {}
    print("PASS: scanning a nonexistent directory returns an empty dict, no crash")


def test_scan_skills_one_malformed_skill_does_not_take_down_others():
    """THE real bug this module was built to fix, tested directly: a
    malformed SKILL.md (missing closing delimiter) sitting alongside two
    perfectly valid ones must not prevent the valid ones from loading."""
    _reset()
    _write_skill("good-skill-one", "First good skill", "Do X")
    (SCRATCH_SKILLS_DIR / "broken-skill").mkdir()
    (SCRATCH_SKILLS_DIR / "broken-skill" / "SKILL.md").write_text(
        "---\nname: broken\nno closing delimiter at all\n", encoding="utf-8"
    )
    _write_skill("good-skill-two", "Second good skill", "Do Y")

    result = skills.scan_skills(skills_dir=SCRATCH_SKILLS_DIR)

    assert "good-skill-one" in result and result["good-skill-one"][0] is not None
    assert "good-skill-two" in result and result["good-skill-two"][0] is not None
    assert "broken-skill" in result and result["broken-skill"][0] is None
    assert result["broken-skill"][1] is not None  # a real error message, not silently dropped
    print(f"PASS: one malformed skill (error: {result['broken-skill'][1][:60]}...) does NOT prevent "
          f"the other 2 valid skills from loading -- exactly the real bug this module fixes")


def test_scan_skills_directory_without_skill_md_is_ignored():
    """A directory that doesn't contain a SKILL.md at all (e.g. some other
    unrelated dotfile-adjacent directory) must be silently skipped, not
    treated as an error."""
    _reset()
    (SCRATCH_SKILLS_DIR / "not-a-skill").mkdir()
    (SCRATCH_SKILLS_DIR / "not-a-skill" / "readme.txt").write_text("nothing to see here")
    _write_skill("real-skill", "A real skill", "body")

    result = skills.scan_skills(skills_dir=SCRATCH_SKILLS_DIR)
    assert "not-a-skill" not in result
    assert "real-skill" in result
    print("PASS: a directory without a SKILL.md is silently ignored, not treated as a broken skill")


# ---------------------------------------------------------------------------
# get_metadata_block (Layer 1)
# ---------------------------------------------------------------------------

def test_metadata_block_empty_for_no_skills():
    assert skills.get_metadata_block({}) == ""
    print("PASS: an empty skills dict produces an empty metadata block")


def test_metadata_block_lists_only_valid_skills_not_broken_ones():
    _reset()
    _write_skill("valid-one", "A valid skill", "body")
    scanned = skills.scan_skills(skills_dir=SCRATCH_SKILLS_DIR)
    scanned["broken-one"] = (None, "some parse error")  # simulate a broken skill entry

    block = skills.get_metadata_block(scanned)
    assert "valid-one: A valid skill" in block
    assert "broken-one" not in block, "a broken skill must not appear in the cheap metadata block shown to every task"
    print("PASS: get_metadata_block lists only successfully-parsed skills, omitting broken ones")


# ---------------------------------------------------------------------------
# render_loaded_skill (Layer 2 + pointer to Layer 3)
# ---------------------------------------------------------------------------

def test_render_loaded_skill_includes_body():
    _reset()
    skill_dir = _write_skill("my-skill", "desc", "Follow these steps:\n1. Do X\n2. Do Y")
    skill = skills.parse_skill_text((skill_dir / "SKILL.md").read_text(), root=skill_dir)
    rendered = skills.render_loaded_skill(skill)
    assert "Follow these steps" in rendered
    assert "1. Do X" in rendered
    print("PASS: render_loaded_skill includes the full skill body")


def test_render_loaded_skill_mentions_supporting_files():
    _reset()
    skill_dir = _write_skill("templated-skill", "desc", "Use the template.")
    (skill_dir / "templates").mkdir()
    (skill_dir / "templates" / "Component.tsx.template").write_text("// template content")

    skill = skills.parse_skill_text((skill_dir / "SKILL.md").read_text(), root=skill_dir)
    rendered = skills.render_loaded_skill(skill)
    assert "templates/Component.tsx.template" in rendered
    assert "read_file" in rendered  # points the model at HOW to load it, not auto-loading it
    print("PASS: render_loaded_skill mentions supporting files by relative path, pointing to read_file (never auto-loading them)")


def test_render_loaded_skill_never_auto_loads_supporting_file_content():
    """Layer 3 must NEVER be auto-loaded -- confirmed the actual template
    file's content ('// template content') does not appear anywhere in
    the rendered output, only its path."""
    _reset()
    skill_dir = _write_skill("templated-skill-2", "desc", "Use the template.")
    (skill_dir / "templates").mkdir()
    (skill_dir / "templates" / "Component.tsx.template").write_text("UNIQUE_MARKER_CONTENT_12345")

    skill = skills.parse_skill_text((skill_dir / "SKILL.md").read_text(), root=skill_dir)
    rendered = skills.render_loaded_skill(skill)
    assert "UNIQUE_MARKER_CONTENT_12345" not in rendered, "Layer 3 content must NEVER be auto-loaded into Layer 2's output"
    print("PASS: a supporting file's actual CONTENT is never auto-loaded -- only its path is mentioned")


def test_render_loaded_skill_shows_tools_hint_as_advisory():
    _reset()
    skill_dir = _write_skill("restricted-skill", "desc", "body", extra_frontmatter="disallowed-tools: Bash, Write\n")
    skill = skills.parse_skill_text((skill_dir / "SKILL.md").read_text(), root=skill_dir)
    rendered = skills.render_loaded_skill(skill)
    assert "Bash, Write" in rendered
    assert "ADVISORY" in rendered or "not an enforced restriction" in rendered
    print("PASS: a tools_hint is shown, explicitly labeled as advisory (not an enforced restriction)")


# ---------------------------------------------------------------------------
# Tool wrappers + registration
# ---------------------------------------------------------------------------

def test_skills_registered_in_tools_registry():
    assert tools.SKILLS_AVAILABLE is True
    assert "load_skill" in tools.TOOL_FUNCTIONS
    assert "list_skills" in tools.TOOL_FUNCTIONS
    spec_names = {s["function"]["name"] for s in tools.TOOL_SPECS}
    assert "load_skill" in spec_names
    assert "list_skills" in spec_names
    print("PASS: load_skill/list_skills are registered in tools.TOOL_FUNCTIONS/TOOL_SPECS")


def test_load_skill_tool_returns_error_for_unknown_name():
    _reset()  # empty real .agent_skills-equivalent for this test's purposes
    result = skills._tool_load_skill("nonexistent-skill-xyz")
    assert result.startswith("ERROR")
    assert "nonexistent-skill-xyz" in result
    print("PASS: load_skill tool returns a clear ERROR for an unknown skill name")


def test_load_skill_tool_returns_error_with_reason_for_broken_skill():
    """End-to-end through the real tool wrapper (_tool_load_skill), not
    just scan_skills directly -- uses the REAL .agent_skills directory
    tools._resolve/WORKDIR points at, via a temporary skill written there
    and cleaned up immediately after.

    Looked up by DIRECTORY name, not the frontmatter's 'name' field --
    this is deliberate, not a workaround: when parsing fails, the intended
    'name' was never successfully extracted at all (that's WHY it failed),
    so scan_skills() can only ever key a broken skill's entry by its
    directory name -- confirmed directly this is the actual, correct
    behavior (an initial version of this test incorrectly assumed lookup
    by the frontmatter's intended name would work even though parsing
    failed before that name was ever extracted)."""
    real_skills_dir = skills._skills_dir()
    test_skill_dir = real_skills_dir / "_test_broken_skill_temp"
    test_skill_dir.mkdir(parents=True, exist_ok=True)
    (test_skill_dir / "SKILL.md").write_text("---\nname: temp\nno closing delimiter\n", encoding="utf-8")
    try:
        result = skills._tool_load_skill("_test_broken_skill_temp")
        assert result.startswith("ERROR"), f"expected an ERROR, got: {result}"
        assert "failed to load" in result
        print(f"PASS: load_skill tool reports a broken skill's error clearly (looked up by directory name): {result}")
    finally:
        import shutil as _shutil
        _shutil.rmtree(test_skill_dir)


def test_list_skills_tool_shows_errors_for_broken_skills():
    real_skills_dir = skills._skills_dir()
    test_skill_dir = real_skills_dir / "_test_list_broken_temp"
    test_skill_dir.mkdir(parents=True, exist_ok=True)
    (test_skill_dir / "SKILL.md").write_text("---\nname: temp2\nno closing delimiter\n", encoding="utf-8")
    try:
        result = skills._tool_list_skills()
        assert "ERROR" in result
        assert "_test_list_broken_temp" in result
        print("PASS: list_skills tool shows a clear ERROR entry for a broken skill, not silence")
    finally:
        import shutil as _shutil
        _shutil.rmtree(test_skill_dir)


def test_tool_spec_schema_matches_documented_parameters():
    """Confirms the LLM-visible schema only exposes what's documented --
    no internal implementation details leaked into the tool spec."""
    load_spec = next(s for s in skills.TOOL_SPECS if s["function"]["name"] == "load_skill")
    props = load_spec["function"]["parameters"]["properties"]
    assert set(props.keys()) == {"name"}
    list_spec = next(s for s in skills.TOOL_SPECS if s["function"]["name"] == "list_skills")
    assert list_spec["function"]["parameters"]["properties"] == {}
    print("PASS: load_skill/list_skills schemas expose only their documented parameters")


if __name__ == "__main__":
    test_parse_normal_skill()
    test_parse_body_containing_literal_horizontal_rule()
    test_parse_missing_closing_delimiter_raises_clear_error()
    test_parse_no_frontmatter_at_all_raises_clear_error()
    test_parse_malformed_yaml_raises_clear_error()
    test_parse_missing_required_name_raises_clear_error()
    test_parse_missing_required_description_raises_clear_error()
    test_parse_tools_hint_string_form()
    test_parse_tools_hint_list_form()
    test_parse_allowed_tools_fallback_when_no_disallowed()
    test_scan_skills_empty_directory_returns_empty_dict()
    test_scan_skills_nonexistent_directory_returns_empty_dict()
    test_scan_skills_one_malformed_skill_does_not_take_down_others()
    test_scan_skills_directory_without_skill_md_is_ignored()
    test_metadata_block_empty_for_no_skills()
    test_metadata_block_lists_only_valid_skills_not_broken_ones()
    test_render_loaded_skill_includes_body()
    test_render_loaded_skill_mentions_supporting_files()
    test_render_loaded_skill_never_auto_loads_supporting_file_content()
    test_render_loaded_skill_shows_tools_hint_as_advisory()
    test_skills_registered_in_tools_registry()
    test_load_skill_tool_returns_error_for_unknown_name()
    test_load_skill_tool_returns_error_with_reason_for_broken_skill()
    test_list_skills_tool_shows_errors_for_broken_skills()
    test_tool_spec_schema_matches_documented_parameters()
    _reset()
    if SCRATCH_SKILLS_DIR.exists():
        import shutil as _shutil
        _shutil.rmtree(SCRATCH_SKILLS_DIR)
    print("\nALL TESTS PASSED")
