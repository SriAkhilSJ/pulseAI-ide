"""
Direct tests for main.py's `_try_expand_slash_invocation` -- direct
`/name [args]` invocation of a skill or plugin command at the REPL, WITHOUT
running the actual interactive loop or calling any real LLM. Isolates the
expansion logic from the ReAct loop itself, same philosophy as
test/main_permission_mode_cli_test.py.

This is the one genuinely new capability plugins.py's own module
docstring identifies (decision 1): Claude Code's current docs say
"custom commands have been merged into skills," so this project already
had the underlying mechanism (skills.py) -- what it never had was a way
for a HUMAN typing at the prompt to force a specific skill/command to run
without waiting for the LLM to decide to call load_skill itself.

Run with: PYTHONPATH=/home/user/my-agent python3 test/main_slash_invocation_test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import plugins  # noqa: E402


def test_builtin_commands_are_never_shadowed():
    for builtin in ("/exit", "/quit", "/reset", "/memory"):
        result = main._try_expand_slash_invocation(builtin)
        assert result is None, f"built-in {builtin} must never be expanded as a skill/command"
    print("PASS: /exit, /quit, /reset, /memory are never shadowed by skill/command expansion")


def test_non_slash_input_returns_none():
    assert main._try_expand_slash_invocation("just a plain message") is None
    print("PASS: plain (non-/) input is never treated as an invocation")


def test_real_committed_skill_expands_correctly():
    """Uses the REAL, already-committed .agent_skills/react-component
    skill -- not a synthetic fixture -- to prove this works against this
    project's actual shipped content."""
    result = main._try_expand_slash_invocation("/react-component build a login form")
    assert result is not None
    assert "functional components" in result.lower() or "hooks" in result.lower()
    assert "Additional arguments from the user: build a login form" in result
    print("PASS: /react-component (a real, committed skill) expands into a real prompt with trailing args appended")


def test_skill_invocation_without_args_has_no_trailing_args_text():
    result = main._try_expand_slash_invocation("/react-component")
    assert result is not None
    assert "Additional arguments from the user" not in result
    print("PASS: invoking a skill with no trailing args produces no spurious 'Additional arguments' text")


def test_unknown_name_returns_none():
    result = main._try_expand_slash_invocation("/this-does-not-exist-anywhere")
    assert result is None
    print("PASS: an unknown /name returns None (caller shows the 'not found' message, not a crash)")


def test_plugin_provided_command_is_directly_invocable():
    with tempfile.TemporaryDirectory() as d:
        workdir = Path(d)
        plugin_root = workdir / plugins.PLUGINS_DIR_NAME / "demo-plugin"
        (plugin_root / ".agent_plugin").mkdir(parents=True)
        (plugin_root / ".agent_plugin" / "plugin.json").write_text(json.dumps({"name": "demo-plugin"}))
        (plugin_root / "commands").mkdir()
        (plugin_root / "commands" / "optimize.md").write_text("Analyze this code for performance issues.")

        with patch.object(plugins, "_get_tools") as mock_get_tools:
            mock_tools = MagicMock()
            mock_tools.WORKDIR = workdir
            mock_get_tools.return_value = mock_tools

            result = main._try_expand_slash_invocation("/optimize")
            assert result == "Analyze this code for performance issues."
    print("PASS: a plugin-provided commands/*.md file is directly invocable via /name, same as a project skill")


def test_plugin_provided_skill_is_directly_invocable():
    with tempfile.TemporaryDirectory() as d:
        workdir = Path(d)
        plugin_root = workdir / plugins.PLUGINS_DIR_NAME / "demo-plugin-2"
        (plugin_root / ".agent_plugin").mkdir(parents=True)
        (plugin_root / ".agent_plugin" / "plugin.json").write_text(json.dumps({"name": "demo-plugin-2"}))
        (plugin_root / "skills" / "greet").mkdir(parents=True)
        (plugin_root / "skills" / "greet" / "SKILL.md").write_text(
            "---\nname: greet\ndescription: Says hello.\n---\nSay a friendly hello."
        )

        with patch.object(plugins, "_get_tools") as mock_get_tools:
            mock_tools = MagicMock()
            mock_tools.WORKDIR = workdir
            mock_get_tools.return_value = mock_tools

            result = main._try_expand_slash_invocation("/greet")
            assert result == "Say a friendly hello."
    print("PASS: a plugin-provided skills/*/SKILL.md is directly invocable via /name")


def test_project_skill_takes_precedence_over_plugin_of_the_same_name():
    """Real precedence decision, made explicit: the project's own
    .agent_skills/ is checked BEFORE any plugin's skills/commands (see
    find_invocable_skill's own ordering) -- a plugin cannot silently
    shadow a project-authored skill of the same name."""
    with tempfile.TemporaryDirectory() as d:
        workdir = Path(d)
        import skills as skills_module
        project_skill_dir = workdir / skills_module.SKILLS_DIR_NAME / "shared-name"
        project_skill_dir.mkdir(parents=True)
        (project_skill_dir / "SKILL.md").write_text(
            "---\nname: shared-name\ndescription: The real project skill.\n---\nPROJECT VERSION."
        )

        plugin_root = workdir / plugins.PLUGINS_DIR_NAME / "shadow-plugin"
        (plugin_root / "skills" / "shared-name").mkdir(parents=True)
        (plugin_root / "skills" / "shared-name" / "SKILL.md").write_text(
            "---\nname: shared-name\ndescription: A plugin trying to shadow it.\n---\nPLUGIN VERSION."
        )

        with patch.object(plugins, "_get_tools") as mock_get_tools, \
             patch.object(skills_module, "_get_tools") as mock_get_tools_skills:
            mock_tools = MagicMock()
            mock_tools.WORKDIR = workdir
            mock_get_tools.return_value = mock_tools
            mock_get_tools_skills.return_value = mock_tools

            skill = plugins.find_invocable_skill("shared-name")
            assert skill is not None
            assert skill.body == "PROJECT VERSION.", (
                f"expected the project's own skill to take precedence, got body: {skill.body!r}"
            )
    print("PASS: a project-authored skill takes precedence over a plugin's skill of the same name")


if __name__ == "__main__":
    test_builtin_commands_are_never_shadowed()
    test_non_slash_input_returns_none()
    test_real_committed_skill_expands_correctly()
    test_skill_invocation_without_args_has_no_trailing_args_text()
    test_unknown_name_returns_none()
    test_plugin_provided_command_is_directly_invocable()
    test_plugin_provided_skill_is_directly_invocable()
    test_project_skill_takes_precedence_over_plugin_of_the_same_name()
    print("\nALL TESTS PASSED")
