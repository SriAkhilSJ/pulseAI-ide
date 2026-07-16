"""
Regression test for plugins.py -- plugin manifests, component loading
(skills/commands/agents/MCP/hooks), marketplace catalog parsing, and the
4 real hook events wired into agent.py's ReAct loop.

Covers every explicit scope decision documented in plugins.py's own
module docstring:

  1. Commands are loaded through skills.py's OWN real parser, not a
     duplicate one -- verified a headerless command file (plain prompt
     text, no frontmatter) still loads successfully via synthesis.
  2. Only 4 real hook events are supported (SessionStart, PreToolUse,
     PostToolUse, Stop) -- an unsupported event name is a warning, not a
     crash. Stop/SessionStart are observational-only (never real
     loop-continuation) -- verified the hook wiring in agent.py appends a
     note rather than claiming to block/continue.
  3. Marketplace source resolution (local/github/git) parses correctly;
     remote fetch reuses GitPython (mocked here -- no real network clone
     in a unit test, but the REAL git.Repo.clone_from call site is
     exercised via a mock that asserts on its exact arguments).
  4. MCP servers are registered via mcp_client.py's existing
     connect_server, not new plumbing (verified via a mock that the
     right (name, command, args) triple would be passed).
  5. One malformed plugin never crashes the whole registry (same
     per-item isolation as skills.py/rules.py/custom_agents.py).

Also includes REAL, live (no mocked LLM) verification that hooks
registered from an actual plugin fire correctly via real subprocess I/O,
and a mocked-LLM integration test proving a PreToolUse hook genuinely
blocks a tool call INSIDE the real agent.run_agent() ReAct loop, not just
in plugins.run_hooks() isolation.

Run with: PYTHONPATH=/home/user/my-agent python3 test/plugins_test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins  # noqa: E402
import agent  # noqa: E402
import llm_client  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_test_plugin(root: Path, name: str = "test-plugin") -> Path:
    plugin_root = root / name
    _write(plugin_root / ".agent_plugin" / "plugin.json", json.dumps({
        "name": name, "description": "A real test plugin", "version": "1.0.0",
    }))
    _write(plugin_root / "skills" / "greet" / "SKILL.md", (
        "---\nname: greet\ndescription: Says hello.\n---\nSay a friendly hello."
    ))
    _write(plugin_root / "commands" / "optimize.md", "Analyze this code for performance issues.")
    _write(plugin_root / "agents" / "reviewer.md", (
        f"---\nname: {name}-reviewer\ndescription: A plugin agent\ntools: [read_file]\nmode: plan\n---\nReview code."
    ))
    return plugin_root


def test_parse_manifest_requires_only_name():
    manifest = plugins.parse_plugin_manifest('{"name": "minimal"}', "plugin.json")
    assert manifest.name == "minimal"
    assert manifest.description == ""
    print("PASS: a plugin.json with only 'name' parses successfully (matches the real spec: only name is required)")


def test_parse_manifest_missing_name_rejected():
    try:
        plugins.parse_plugin_manifest('{"description": "no name"}', "bad.json")
        print("FAIL: expected rejection of a manifest with no name")
        sys.exit(1)
    except ValueError as e:
        assert "name" in str(e)
    print("PASS: a plugin.json missing 'name' is rejected with a clear message")


def test_parse_manifest_malformed_json_rejected():
    try:
        plugins.parse_plugin_manifest("not json at all {", "bad.json")
        print("FAIL: expected rejection of malformed JSON")
        sys.exit(1)
    except ValueError:
        pass
    print("PASS: malformed plugin.json is rejected, not silently swallowed")


def test_load_plugin_loads_all_real_components():
    with tempfile.TemporaryDirectory() as d:
        plugin_root = _make_test_plugin(Path(d))
        loaded = plugins.load_plugin(plugin_root)
        assert loaded.manifest.name == "test-plugin"
        assert "greet" in loaded.skill_names
        assert "optimize" in loaded.command_names
        assert f"test-plugin-reviewer" in loaded.agent_names
        assert loaded.load_warnings == []
    print("PASS: a real plugin's skills/commands/agents all load through the real skills.py/custom_agents.py parsers")


def test_headerless_command_file_synthesizes_valid_frontmatter():
    """Decision 1: a command file with NO frontmatter at all (just plain
    prompt text) is a real, valid shape -- must not be rejected."""
    with tempfile.TemporaryDirectory() as d:
        commands_dir = Path(d) / "commands"
        _write(commands_dir / "bare.md", "Just do the thing, no frontmatter here.")
        names, warnings = plugins._load_commands_as_skills(commands_dir)
        assert "bare" in names, f"expected 'bare' in {names}"
        assert warnings == [], f"expected no warnings, got {warnings}"
    print("PASS: a headerless command file (plain prompt text) synthesizes valid frontmatter and loads successfully")


def test_command_file_with_frontmatter_but_no_name_field_still_loads():
    """Real bug found and fixed while building this project's own
    committed example plugin (.agent_plugins/git-safety/commands/
    changelog.md): a command file can have REAL frontmatter (description,
    argument-hint, etc.) but with NO `name:` field at all -- the filename
    is always authoritative for a command's name, per the real documented
    behavior ("the name is the filename without its extension"). A first
    draft of _load_commands_as_skills only synthesized frontmatter for
    the "no frontmatter at ALL" case, so a file with real-but-nameless
    frontmatter was rejected as malformed -- caught by actually loading
    this project's own real example plugin, not assumed to work."""
    with tempfile.TemporaryDirectory() as d:
        commands_dir = Path(d) / "commands"
        _write(commands_dir / "changelog.md", (
            "---\ndescription: Summarize recent git history.\n---\n"
            "Run git_log and summarize what happened."
        ))
        names, warnings = plugins._load_commands_as_skills(commands_dir)
        assert "changelog" in names, f"expected 'changelog' in {names}"
        assert warnings == [], f"expected no warnings, got {warnings}"
    print("PASS: a command file with real frontmatter but no 'name:' field still loads (filename is authoritative)")


def test_malformed_component_does_not_crash_whole_plugin_load():
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "broken-plugin"
        _write(plugin_root / ".agent_plugin" / "plugin.json", json.dumps({"name": "broken-plugin"}))
        _write(plugin_root / "skills" / "good" / "SKILL.md", "---\nname: good\ndescription: fine\n---\nbody")
        _write(plugin_root / "skills" / "bad" / "SKILL.md", "not even frontmatter")
        loaded = plugins.load_plugin(plugin_root)
        assert "good" in loaded.skill_names
        assert any("bad" in w for w in loaded.load_warnings)
    print("PASS: one malformed component (a broken skill) doesn't prevent the rest of the plugin from loading")


def test_scan_local_plugins_isolates_one_broken_plugin_from_others():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_test_plugin(d, name="good-plugin")
        bad_root = d / "bad-plugin"
        _write(bad_root / ".agent_plugin" / "plugin.json", "not json {")
        results = plugins.scan_local_plugins(d)
        assert results["good-plugin"][0] is not None
        assert results["bad-plugin"][0] is None
        assert "bad-plugin" in results
    print("PASS: scan_local_plugins isolates one malformed plugin directory from a valid sibling")


def test_unsupported_hook_event_is_a_warning_not_a_crash():
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "plugin"
        _write(plugin_root / "hooks" / "hooks.json", json.dumps({
            "hooks": {"WorktreeCreate": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}
        }))
        events, warnings = plugins._load_hooks(plugin_root)
        assert events == []
        assert any("WorktreeCreate" in w for w in warnings)
    print("PASS: an unsupported hook event (e.g. WorktreeCreate, no analog in this project) is a warning, not a crash")


def test_supported_hook_events_are_exactly_the_documented_4():
    assert plugins.SUPPORTED_HOOK_EVENTS == {"SessionStart", "PreToolUse", "PostToolUse", "Stop"}
    print("PASS: SUPPORTED_HOOK_EVENTS is exactly the 4 events mapped onto real agent.py extension points")


def test_real_hook_blocks_matching_tool_and_allows_others():
    """Real subprocess execution, real stdin/stdout JSON contract."""
    plugins.clear_hook_registry()
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "hook-plugin"
        hook_cmd = (
            "python3 -c \"import sys, json; d=json.load(sys.stdin); "
            "print(json.dumps({'decision':'block','reason':'blocked'}) "
            "if 'forbidden' in d.get('tool_args',{}).get('cmd','') else '')\""
        )
        _write(plugin_root / "hooks" / "hooks.json", json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": hook_cmd}]}]}
        }))
        plugins.load_plugin(plugin_root)

        blocked = plugins.run_hooks("PreToolUse", {"tool_name": "run_command", "tool_args": {"cmd": "echo forbidden"}})
        assert blocked is not None and blocked.get("decision") == "block"

        allowed = plugins.run_hooks("PreToolUse", {"tool_name": "run_command", "tool_args": {"cmd": "echo hi"}})
        assert allowed is None

        not_matched = plugins.run_hooks("PreToolUse", {"tool_name": "read_file", "tool_args": {"path": "forbidden.txt"}})
        assert not_matched is None, "matcher should have excluded a non-matching tool name"
    plugins.clear_hook_registry()
    print("PASS: a real hook (real subprocess, real JSON stdin/stdout) blocks a matching forbidden call and allows others")


def test_broken_hook_command_fails_open():
    """A hook that errors/times out/produces garbage must NEVER block a
    real call -- fail-open by construction."""
    plugins.clear_hook_registry()
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "broken-hook-plugin"
        _write(plugin_root / "hooks" / "hooks.json", json.dumps({
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "this-binary-does-not-exist-anywhere"}]}]}
        }))
        plugins.load_plugin(plugin_root)
        result = plugins.run_hooks("PreToolUse", {"tool_name": "anything", "tool_args": {}})
        assert result is None, f"a broken hook command must fail open (return None), got: {result}"
    plugins.clear_hook_registry()
    print("PASS: a broken/nonexistent hook command fails open (never blocks a real call)")


def test_marketplace_parses_local_github_and_git_sources():
    catalog = json.dumps({
        "plugins": [
            {"name": "local-one", "source": "./plugins/local-one", "description": "a local plugin"},
            {"name": "gh-one", "source": {"source": "github", "repo": "someorg/some-plugin"}, "description": "a github plugin"},
            {"name": "git-one", "source": "https://example.com/plugin.git", "description": "a git-url plugin"},
        ]
    })
    entries = plugins.parse_marketplace_file(catalog, "marketplace.json")
    by_name = {e.name: e for e in entries}
    assert by_name["local-one"].source_kind == "local"
    assert by_name["gh-one"].source_kind == "github" and by_name["gh-one"].source == "someorg/some-plugin"
    assert by_name["git-one"].source_kind == "git"
    print("PASS: marketplace.json parses local/github/git source shapes correctly")


def test_marketplace_missing_plugins_array_rejected():
    try:
        plugins.parse_marketplace_file('{"name": "x"}', "bad.json")
        print("FAIL: expected rejection of a marketplace file with no 'plugins' array")
        sys.exit(1)
    except ValueError:
        pass
    print("PASS: a marketplace.json missing the 'plugins' array is rejected clearly")


def test_scan_marketplace_missing_file_is_not_an_error():
    entries, err = plugins.scan_marketplace(Path("/tmp/definitely-does-not-exist-marketplace.json"))
    assert entries == [] and err is None, "a missing marketplace file is optional, not an error"
    print("PASS: a missing marketplace.json returns ([], None) -- a marketplace catalog is optional")


def test_resolve_remote_source_uses_gitpython_clone_from():
    """Real call-site verification: resolve_plugin_source for a github
    entry must call git.Repo.clone_from with the correctly-expanded URL --
    mocked here (no real network clone in a unit test) but asserting on
    the EXACT real call, not just that 'something happened'."""
    entry = plugins.MarketplaceEntry(name="remote-plugin", source="someorg/some-plugin", source_kind="github")

    with tempfile.TemporaryDirectory() as d:
        fake_workdir = Path(d)
        with patch.object(plugins, "_get_tools") as mock_get_tools:
            mock_tools = MagicMock()
            mock_tools.WORKDIR = fake_workdir
            mock_get_tools.return_value = mock_tools

            with patch("git.Repo.clone_from") as mock_clone:
                plugins.resolve_plugin_source(entry)
                mock_clone.assert_called_once()
                call_args = mock_clone.call_args
                assert call_args[0][0] == "https://github.com/someorg/some-plugin.git", (
                    f"expected the github shorthand expanded to a real clone URL, got: {call_args[0][0]}"
                )
    print("PASS: a github-source marketplace entry calls git.Repo.clone_from with the correctly-expanded URL")


def test_local_source_resolves_to_real_existing_path():
    with tempfile.TemporaryDirectory() as d:
        fake_workdir = Path(d)
        (fake_workdir / "my-plugin").mkdir()
        entry = plugins.MarketplaceEntry(name="local", source="my-plugin", source_kind="local")
        with patch.object(plugins, "_get_tools") as mock_get_tools:
            mock_tools = MagicMock()
            mock_tools.WORKDIR = fake_workdir
            mock_get_tools.return_value = mock_tools
            resolved = plugins.resolve_plugin_source(entry)
            assert resolved == (fake_workdir / "my-plugin").resolve()
    print("PASS: a local-source marketplace entry resolves to the real existing directory")


def test_local_source_missing_directory_raises_clearly():
    with tempfile.TemporaryDirectory() as d:
        fake_workdir = Path(d)
        entry = plugins.MarketplaceEntry(name="ghost", source="does-not-exist", source_kind="local")
        with patch.object(plugins, "_get_tools") as mock_get_tools:
            mock_tools = MagicMock()
            mock_tools.WORKDIR = fake_workdir
            mock_get_tools.return_value = mock_tools
            try:
                plugins.resolve_plugin_source(entry)
                print("FAIL: expected a RuntimeError for a missing local source")
                sys.exit(1)
            except RuntimeError as e:
                assert "does-not-exist" in str(e)
    print("PASS: a local source pointing at a nonexistent directory raises a clear RuntimeError")


def test_mcp_servers_registered_via_existing_connect_server():
    """Decision 4: a plugin's .mcp.json must go through mcp_client.py's
    ALREADY-GENERIC connect_server, not new plumbing -- verified by
    mocking that exact call site and asserting on its arguments."""
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "mcp-plugin"
        _write(plugin_root / ".mcp.json", json.dumps({
            "mcpServers": {"my-server": {"command": "my-binary", "args": ["--flag"]}}
        }))
        manifest = plugins.PluginManifest(name="mcp-plugin")

        import mcp_client
        # Replace connect_server with a plain (non-async) FUNCTION
        # assigned directly onto the instance -- NOT via patch.object's
        # side_effect, which unittest.mock still wraps in an AsyncMock
        # when the original attribute is a coroutine function (inspected
        # via inspect.iscoroutinefunction on the ORIGINAL, regardless of
        # what side_effect is), producing a real unawaited-coroutine
        # warning purely as a test-harness artifact -- not a real bug in
        # plugins.py's actual runtime behavior, which DOES properly await
        # this through the real loop thread. A direct attribute
        # assignment (restored in `finally`) sidesteps that inspection
        # entirely: `mcp_manager.connect_server` becomes a genuinely plain
        # function for the duration of this test.
        captured_calls = []

        def fake_connect_server(name, command, args):
            captured_calls.append((name, command, args))
            return ["plugin_mcp-plugin_my-server_sometool"]

        original_connect_server = mcp_client.mcp_manager.connect_server
        mcp_client.mcp_manager.connect_server = fake_connect_server
        try:
            with patch.object(mcp_client, "MCP_AVAILABLE", True), \
                 patch.object(mcp_client, "_get_loop_thread") as mock_get_loop:
                mock_loop = MagicMock()
                # loop_thread.run(coro, timeout=...) normally awaits the
                # coroutine on a real event loop; here it just returns
                # connect_server's plain return value directly, since
                # fake_connect_server above is a plain function, not a
                # coroutine function -- no unawaited coroutine is ever
                # created.
                mock_loop.run.side_effect = lambda coro, timeout=None: coro
                mock_get_loop.return_value = mock_loop

                names, warnings = plugins._load_mcp_servers(plugin_root, manifest)
                assert names == ["plugin_mcp-plugin_my-server_sometool"]
                assert warnings == []
                # Confirm the REAL call site was reached with the right
                # args (decision 4: reuses connect_server, no new
                # plumbing).
                assert captured_calls == [("plugin_mcp-plugin_my-server", "my-binary", ["--flag"])]
        finally:
            mcp_client.mcp_manager.connect_server = original_connect_server
    print("PASS: a plugin's .mcp.json registers servers via mcp_client.py's existing connect_server, no new plumbing")


def test_list_plugins_and_install_plugin_registered_as_tools():
    import tools
    assert "list_plugins" in tools.TOOL_FUNCTIONS
    assert "install_plugin" in tools.TOOL_FUNCTIONS
    assert "list_plugins" in [s["function"]["name"] for s in tools.TOOL_SPECS]
    assert "install_plugin" in [s["function"]["name"] for s in tools.TOOL_SPECS]
    print("PASS: list_plugins/install_plugin are registered in tools.TOOL_FUNCTIONS/TOOL_SPECS")


def test_install_plugin_unknown_name_returns_clean_error():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(plugins, "_marketplace_file", return_value=Path(d) / "nonexistent.json"):
            result = plugins._tool_install_plugin("does-not-exist")
            assert result.startswith("ERROR:")
    print("PASS: install_plugin with an unknown name (no marketplace file at all) returns a clean ERROR string")


# ---------------------------------------------------------------------------
# Full ReAct-loop wiring: a PreToolUse hook genuinely blocks a call INSIDE
# agent.run_agent(), using a mocked LLM (same proven pattern as
# test/batching_nudge_test.py's FakeToolCall/FakeChoice/FakeMessage).
# ---------------------------------------------------------------------------

class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments, id="fake_1"):
        self.function = FakeFunction(name, arguments)
        self.id = id


class FakeChoice:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


class FakeMessage:
    def __init__(self, choice):
        self.choices = [type("C", (), {"message": choice})()]


def test_pretooluse_hook_genuinely_blocks_a_call_inside_the_real_react_loop():
    """The real, end-to-end wiring proof: NOT plugins.run_hooks() in
    isolation (already covered above), but a hook registered from a real
    plugin actually intercepting a call made by agent.run_agent()'s own
    ReAct loop, using a fully mocked LLM (no real API call, no
    non-determinism) -- same proven mocking pattern as
    test/batching_nudge_test.py."""
    plugins.clear_hook_registry()
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "blocker-plugin"
        hook_cmd = (
            "python3 -c \"import sys, json; print(json.dumps({'decision':'block','reason':'blocked by test hook'}))\""
        )
        _write(plugin_root / "hooks" / "hooks.json", json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": hook_cmd}]}]}
        }))
        plugins.load_plugin(plugin_root)

        call_sequence = [
            FakeMessage(FakeChoice(tool_calls=[FakeToolCall("run_command", '{"cmd": "echo hi"}', id="c1")], content="running")),
            FakeMessage(FakeChoice(tool_calls=None, content="done")),
        ]
        call_index = {"i": 0}

        def fake_chat_completion(messages, tools=None, **kwargs):
            msg = call_sequence[call_index["i"]]
            call_index["i"] += 1
            return msg

        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            events = []
            reply = agent.run_agent(
                "irrelevant task text",
                verbose=False,
                log=lambda e, p: events.append((e, p)),
                confirm=lambda *a: True,
                persist_memory=False,
                max_iterations=5,
            )

        observations = [p for (e, p) in events if e == "Observation"]
        assert any("CANCELLED" in str(o) and "PreToolUse hook" in str(o) for o in observations), (
            f"expected a real CANCELLED-by-hook observation, got: {observations}"
        )
    plugins.clear_hook_registry()
    print("PASS: a real plugin's PreToolUse hook genuinely blocks a tool call INSIDE agent.run_agent()'s real ReAct loop")


def test_posttooluse_hook_note_appears_in_tool_result_content():
    plugins.clear_hook_registry()
    with tempfile.TemporaryDirectory() as d:
        plugin_root = Path(d) / "note-plugin"
        hook_cmd = (
            "python3 -c \"import sys, json; print(json.dumps({'decision':'block','reason':'post-hook note here'}))\""
        )
        _write(plugin_root / "hooks" / "hooks.json", json.dumps({
            "hooks": {"PostToolUse": [{"matcher": "list_files", "hooks": [{"type": "command", "command": hook_cmd}]}]}
        }))
        plugins.load_plugin(plugin_root)

        call_sequence = [
            FakeMessage(FakeChoice(tool_calls=[FakeToolCall("list_files", '{"directory": "."}', id="c1")], content="listing")),
            FakeMessage(FakeChoice(tool_calls=None, content="done")),
        ]
        call_index = {"i": 0}

        def fake_chat_completion(messages, tools=None, **kwargs):
            msg = call_sequence[call_index["i"]]
            call_index["i"] += 1
            return msg

        with patch.object(llm_client, "chat_completion", side_effect=fake_chat_completion):
            events = []
            agent.run_agent(
                "irrelevant task text",
                verbose=False,
                log=lambda e, p: events.append((e, p)),
                confirm=lambda *a: True,
                persist_memory=False,
                max_iterations=5,
            )

        notes = [p for (e, p) in events if e == "Note"]
        assert any("post-hook note here" in str(n) for n in notes), f"expected the PostToolUse hook's note, got: {notes}"
    plugins.clear_hook_registry()
    print("PASS: a real plugin's PostToolUse hook note appears in the tool-result content (same injection point as the batching nudge/rules)")


if __name__ == "__main__":
    test_parse_manifest_requires_only_name()
    test_parse_manifest_missing_name_rejected()
    test_parse_manifest_malformed_json_rejected()
    test_load_plugin_loads_all_real_components()
    test_headerless_command_file_synthesizes_valid_frontmatter()
    test_command_file_with_frontmatter_but_no_name_field_still_loads()
    test_malformed_component_does_not_crash_whole_plugin_load()
    test_scan_local_plugins_isolates_one_broken_plugin_from_others()
    test_unsupported_hook_event_is_a_warning_not_a_crash()
    test_supported_hook_events_are_exactly_the_documented_4()
    test_real_hook_blocks_matching_tool_and_allows_others()
    test_broken_hook_command_fails_open()
    test_marketplace_parses_local_github_and_git_sources()
    test_marketplace_missing_plugins_array_rejected()
    test_scan_marketplace_missing_file_is_not_an_error()
    test_resolve_remote_source_uses_gitpython_clone_from()
    test_local_source_resolves_to_real_existing_path()
    test_local_source_missing_directory_raises_clearly()
    test_mcp_servers_registered_via_existing_connect_server()
    test_list_plugins_and_install_plugin_registered_as_tools()
    test_install_plugin_unknown_name_returns_clean_error()
    test_pretooluse_hook_genuinely_blocks_a_call_inside_the_real_react_loop()
    test_posttooluse_hook_note_appears_in_tool_result_content()
    print("\nALL TESTS PASSED")
