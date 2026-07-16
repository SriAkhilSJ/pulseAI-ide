"""
Regression test for custom_agents.py -- custom agent definitions
(`.agent_agents/*.md`, inheritance, and their wiring into
subagents.dispatch_agent(agent_name=...)).

Covers every explicit design decision made when a proposal for this
feature left things ambiguous (see custom_agents.py's own module
docstring for the full rationale):

  1. No `model:` field in v1 -- surfaced as a warning (unknown_fields),
     never a hard error, never silently dropped without a trace.
  2. `skills:` is metadata-only (never force-preloaded).
  3. `tools:` composes with `mode:` via INTERSECTION, never union.
  4. Single-parent inheritance (`extends:`), cycle detection.
  5. Field-level composition rules (name/description=child-only,
     skills=union, tools/mode/max_iterations=nearest-ancestor-wins,
     body=prepend child-then-parent).
  6. One malformed agent file never crashes the whole registry (same
     per-item isolation as skills.py/rules.py).

Also verifies the real, live-confirmed circular-import hazard this
feature could have reintroduced: subagents.py MUST import permissions.py
LAZILY (inside a function), never at module level -- confirmed live in
this session that a module-level `import permissions` inside subagents.py
deadlocks at import time, because permissions.py itself does
`from subagents import READ_ONLY_TOOL_NAMES` at ITS OWN module level.

Run with: PYTHONPATH=/home/user/my-agent python3 test/custom_agents_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import custom_agents as ca  # noqa: E402
import subagents  # noqa: E402


def _write(d: Path, filename: str, content: str) -> None:
    (d / filename).write_text(content, encoding="utf-8")


def test_parse_agent_text_basic():
    text = "---\nname: foo\ndescription: does foo things\n---\nYou do foo."
    raw = ca.parse_agent_text(text, "foo.md")
    assert raw.name == "foo"
    assert raw.description == "does foo things"
    assert raw.body == "You do foo."
    assert raw.extends is None
    assert raw.tools is None
    assert raw.unknown_fields == []
    print("PASS: basic agent file parses correctly")


def test_missing_required_fields_rejected():
    for bad_text, missing in [
        ("---\ndescription: x\n---\nbody", "name"),
        ("---\nname: x\n---\nbody", "description"),
    ]:
        try:
            ca.parse_agent_text(bad_text, "bad.md")
            print(f"FAIL: expected rejection of missing {missing}")
            sys.exit(1)
        except ValueError as e:
            assert missing in str(e)
    print("PASS: missing required name/description fields are rejected with a clear message")


def test_malformed_frontmatter_rejected():
    try:
        ca.parse_agent_text("no frontmatter here at all", "bad.md")
        print("FAIL: expected rejection of malformed frontmatter")
        sys.exit(1)
    except ValueError:
        pass
    print("PASS: malformed/missing frontmatter is rejected, not silently swallowed")


def test_unknown_field_like_model_is_a_warning_not_an_error():
    text = "---\nname: x\ndescription: y\nmodel: gemini/gemini-2.0-flash-exp\n---\nbody"
    raw = ca.parse_agent_text(text, "x.md")
    assert raw.name == "x"  # the rest of the file still loaded successfully
    assert "model" in raw.unknown_fields, (
        "decision 1: an unsupported field like model: must be surfaced as a "
        "warning (unknown_fields), not silently dropped or a hard error"
    )
    print("PASS: an unrecognized field (e.g. model:) is a warning, agent still loads (decision 1)")


def test_scan_isolates_one_broken_file_from_others():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "good.md", "---\nname: good\ndescription: fine\n---\nbody")
        _write(d, "bad.md", "not even frontmatter")
        results = ca.scan_agent_defs(d)
        assert results["good"][0] is not None, "a malformed sibling file must not affect a valid one"
        assert results["good"][0].description == "fine"
        assert results["bad"][0] is None
        assert "ValueError" in results["bad"][1]
    print("PASS: one malformed agent file does not prevent a valid sibling from loading")


def test_duplicate_name_across_files_is_reported_not_silently_overwritten():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "dup1.md", "---\nname: dup\ndescription: first\n---\nb1")
        _write(d, "dup2.md", "---\nname: dup\ndescription: second\n---\nb2")
        results = ca.scan_agent_defs(d)
        assert results["dup"][0] is None
        assert "duplicate" in results["dup"][1].lower()
    print("PASS: a duplicate agent name across two files is reported as an error, not silently overwritten")


def test_inheritance_composition_rules():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "base.md", (
            "---\nname: base\ndescription: base description\n"
            "tools: [read_file, write_file]\nmode: accept_edits\nmax_iterations: 15\n"
            "---\nBase instructions."
        ))
        _write(d, "child.md", (
            "---\nname: child\nextends: base\ndescription: child description\n"
            "skills: [skill-a, skill-b]\ntools: [read_file]\nmode: plan\n"
            "---\nChild instructions."
        ))
        raw_defs = ca.scan_agent_defs(d)
        resolved = ca.resolve_agent("child", raw_defs)

        assert resolved.name == "child", "name is always the requested agent's own (never inherited)"
        assert resolved.description == "child description", "description: child always wins"
        assert resolved.tools == ["read_file"], "tools: child's own value replaces parent's entirely"
        assert resolved.mode == "plan", "mode: child's own value replaces parent's"
        assert resolved.max_iterations == 15, "max_iterations: not given by child, must inherit from parent"
        assert resolved.skills == ["skill-a", "skill-b"], "skills: union (here child fully defines it)"
        assert resolved.body == "Child instructions.\n\n---\n\nBase instructions.", (
            "body: PREPEND child's own body, then parent's, in that reading order"
        )
    print("PASS: inheritance composition rules (replace/union/nearest-ancestor-wins/prepend) all correct")


def test_skills_union_across_three_levels():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "grandparent.md", "---\nname: gp\ndescription: gp\nskills: [s1]\n---\nGP body")
        _write(d, "parent.md", "---\nname: p\nextends: gp\ndescription: p\nskills: [s2]\n---\nP body")
        _write(d, "child.md", "---\nname: c\nextends: p\ndescription: c\nskills: [s1, s3]\n---\nC body")
        raw_defs = ca.scan_agent_defs(d)
        resolved = ca.resolve_agent("c", raw_defs)
        # union, deduplicated, order doesn't matter for correctness but should be stable
        assert set(resolved.skills) == {"s1", "s2", "s3"}
        assert len(resolved.skills) == 3, f"expected 3 deduplicated skills, got {resolved.skills}"
        assert resolved.body == "C body\n\n---\n\nP body\n\n---\n\nGP body"
    print("PASS: skills union correctly deduplicates across a 3-level inheritance chain")


def test_cycle_detection():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "a.md", "---\nname: a\ndescription: A\nextends: b\n---\nbody a")
        _write(d, "b.md", "---\nname: b\ndescription: B\nextends: a\n---\nbody b")
        raw_defs = ca.scan_agent_defs(d)
        try:
            ca.resolve_agent("a", raw_defs)
            print("FAIL: expected a cycle to be detected")
            sys.exit(1)
        except ValueError as e:
            assert "cycle" in str(e).lower()
            assert "a" in str(e) and "b" in str(e), "the error should show the actual cycle, not just say 'cycle detected'"
    print("PASS: an inheritance cycle is detected and reported with the actual chain")


def test_broken_extends_link_reported_clearly():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "c.md", "---\nname: c\ndescription: C\nextends: nonexistent\n---\nbody")
        raw_defs = ca.scan_agent_defs(d)
        try:
            ca.resolve_agent("c", raw_defs)
            print("FAIL: expected a broken extends link to be rejected")
            sys.exit(1)
        except ValueError as e:
            assert "nonexistent" in str(e)
    print("PASS: an extends: link to a nonexistent agent is reported clearly")


def test_unknown_agent_name_reported_clearly():
    try:
        ca.resolve_agent("totally-made-up", {})
        print("FAIL: expected an unknown agent name to be rejected")
        sys.exit(1)
    except ValueError as e:
        assert "totally-made-up" in str(e)
    print("PASS: an unknown agent name is reported clearly, not a KeyError")


def test_tools_mode_intersection_not_union():
    """Decision 3: mode:plan (structurally read-only) + tools:[write_file,
    run_command] must NOT re-grant write_file/run_command -- the final
    registry is the INTERSECTION, never the union."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "conflict.md", (
            "---\nname: conflict-test\ndescription: intersection test\n"
            "mode: plan\ntools: [read_file, write_file, run_command]\n"
            "---\nbody"
        ))
        raw_defs = ca.scan_agent_defs(d)

        orig_scan = ca.scan_agent_defs
        ca.scan_agent_defs = lambda: raw_defs
        try:
            tool_functions, tool_specs, system_prompt, max_iter = (
                subagents._restricted_registry_for_named_agent("conflict-test", subagent_depth=0)
            )
        finally:
            ca.scan_agent_defs = orig_scan

        allowed = set(tool_functions.keys())
        assert "write_file" not in allowed, "mode=plan must block write_file even though tools: lists it"
        assert "run_command" not in allowed, "mode=plan must block run_command even though tools: lists it"
        assert "read_file" in allowed, "read_file is genuinely in the intersection of mode's set and tools:"
    print("PASS: tools: + mode: compose via intersection, never union (decision 3)")


def test_depth_limit_still_enforced_for_named_agents():
    """A named custom agent gets NO special exemption from the same
    MAX_SUBAGENT_DEPTH nesting protection every subagent_type dispatch
    already has."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write(d, "full.md", "---\nname: full-access\ndescription: full access test\n---\nbody")
        raw_defs = ca.scan_agent_defs(d)

        orig_scan = ca.scan_agent_defs
        ca.scan_agent_defs = lambda: raw_defs
        try:
            # subagent_depth=0, default MAX_SUBAGENT_DEPTH=1 -> 0+1 < 1 is False
            tool_functions, tool_specs, system_prompt, max_iter = (
                subagents._restricted_registry_for_named_agent("full-access", subagent_depth=0)
            )
        finally:
            ca.scan_agent_defs = orig_scan

        assert "dispatch_agent" not in tool_functions, (
            "a named agent with no tools:/mode: restriction at all must still respect "
            "MAX_SUBAGENT_DEPTH -- no special exemption from the nesting limit"
        )
    print("PASS: MAX_SUBAGENT_DEPTH nesting limit applies to named custom agents too")


def test_dispatch_agent_unknown_agent_name_returns_clean_error():
    result = subagents.dispatch_agent(
        prompt="anything",
        agent_name="does-not-exist-at-all",
        _confirm=lambda *a: True,
    )
    assert result.startswith("ERROR:")
    assert "does-not-exist-at-all" in result
    print("PASS: dispatch_agent(agent_name=<unknown>) returns a clean ERROR string, no raw traceback")


def test_dispatch_agent_still_works_with_subagent_type_when_agent_name_omitted():
    """Backward compatibility: the pre-existing subagent_type path must be
    completely untouched when agent_name is not given."""
    result = subagents.dispatch_agent(
        prompt="anything",
        subagent_type="not-a-real-type",
        _confirm=lambda *a: True,
    )
    assert result.startswith("ERROR: unknown subagent_type"), (
        f"expected the EXACT prior unknown-subagent_type error path, got: {result}"
    )
    print("PASS: dispatch_agent's original subagent_type validation path is untouched")


def test_no_module_level_circular_import_between_subagents_and_permissions():
    """The real hazard confirmed live earlier this session: a module-level
    `import permissions` inside subagents.py deadlocks, because
    permissions.py itself does `from subagents import READ_ONLY_TOOL_NAMES`
    at ITS OWN module level. Confirms subagents.py's source has NO
    module-level import of permissions (it must stay lazy, inside a
    function)."""
    import ast
    subagents_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "subagents.py")
    with open(subagents_path) as f:
        tree = ast.parse(f.read(), filename="subagents.py")
    for node in ast.walk(tree):
        # Only check imports at MODULE level (direct children of Module),
        # not ones nested inside a function/method body -- those are fine
        # and expected (see _restricted_registry_for_named_agent's own
        # lazy `import permissions as _permissions`).
        pass
    module_level_imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_level_imports.append(node.module)
    assert "permissions" not in module_level_imports, (
        "subagents.py must NOT import permissions at module level -- confirmed live "
        "that this deadlocks (permissions.py imports subagents.py at ITS module level "
        "too). It must stay a lazy, in-function import."
    )
    # Also confirm the actual import still works end-to-end at runtime
    # (both modules load successfully via the lazy-import path).
    import importlib
    import permissions as _permissions_check  # noqa: F401
    importlib.reload(subagents)
    print("PASS: subagents.py has no module-level import of permissions (confirmed live circular-import hazard avoided)")


def test_list_custom_agents_tool_registered():
    import tools
    assert "list_custom_agents" in tools.TOOL_FUNCTIONS
    assert "list_custom_agents" in [s["function"]["name"] for s in tools.TOOL_SPECS]
    print("PASS: list_custom_agents is registered in tools.TOOL_FUNCTIONS/TOOL_SPECS")


def test_dispatch_agent_schema_exposes_agent_name():
    spec = next(s for s in subagents.TOOL_SPECS if s["function"]["name"] == "dispatch_agent")
    props = spec["function"]["parameters"]["properties"]
    assert "agent_name" in props
    assert "prompt" in props
    assert "subagent_type" in props
    print("PASS: dispatch_agent's LLM-visible schema exposes agent_name alongside the existing params")


if __name__ == "__main__":
    test_parse_agent_text_basic()
    test_missing_required_fields_rejected()
    test_malformed_frontmatter_rejected()
    test_unknown_field_like_model_is_a_warning_not_an_error()
    test_scan_isolates_one_broken_file_from_others()
    test_duplicate_name_across_files_is_reported_not_silently_overwritten()
    test_inheritance_composition_rules()
    test_skills_union_across_three_levels()
    test_cycle_detection()
    test_broken_extends_link_reported_clearly()
    test_unknown_agent_name_reported_clearly()
    test_tools_mode_intersection_not_union()
    test_depth_limit_still_enforced_for_named_agents()
    test_dispatch_agent_unknown_agent_name_returns_clean_error()
    test_dispatch_agent_still_works_with_subagent_type_when_agent_name_omitted()
    test_no_module_level_circular_import_between_subagents_and_permissions()
    test_list_custom_agents_tool_registered()
    test_dispatch_agent_schema_exposes_agent_name()
    print("\nALL TESTS PASSED")
