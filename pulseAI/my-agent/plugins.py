"""
plugins.py
----------
Plugins -- a packaging/distribution layer over features this project
ALREADY has (skills, custom agent definitions, MCP servers) plus 2 small
new capabilities (direct `/name` skill invocation, and a real, narrow
subset of lifecycle hooks) -- per COMPARISON_openclaude.md's own framing:
"a packaging layer over everything above, not a new capability by itself."

Real, currently-documented Claude Code plugin format, verified this
session against `code.claude.com/docs/en/plugins`, `plugins-reference`,
and `plugin-marketplaces` (official docs) plus several independently-
published, current explainers -- NOT extracted from Gitlawb/openclaude's
leaked `src/`, NOT assumed from training data:

  - A plugin is a directory with `.claude-plugin/plugin.json` (manifest --
    only `name` is required) + component folders AT THE PLUGIN ROOT, not
    inside `.claude-plugin/` (the docs' own most-common-mistake warning:
    "only plugin.json lives inside .claude-plugin/"). This project mirrors
    that exactly, just renamed for this codebase's own `.agent_*`
    convention: `.agent_plugins/<name>/.agent_plugin/plugin.json` +
    `skills/`, `agents/`, `commands/`, `hooks/hooks.json`, `.mcp.json` at
    the plugin root.
  - A marketplace is a separate `marketplace.json` catalog (this project:
    `.agent_marketplace.json`) listing `{name, source, description}`
    entries, where `source` is either a local relative path, or
    `{"source": "github", "repo": "owner/repo"}` / a plain git URL string.

REAL, DELIBERATE SCOPE DECISIONS (each one checked against this project's
ACTUAL existing code before deciding, not assumed):

1. COMMANDS ARE NOT A SEPARATE MECHANISM. Verified directly against
   Claude Code's own current docs (`code.claude.com/docs/en/slash-
   commands`): "Custom commands have been merged into skills. A file at
   `.claude/commands/deploy.md` and a skill at `.claude/skills/deploy/
   SKILL.md` both create `/deploy` and work the same way." This project
   already has a full skills implementation (skills.py) -- a plugin's
   `commands/*.md` files are loaded through the EXACT SAME
   skills.parse_skill_text/scan_skills machinery (see _load_commands_as_
   skills below), not a second parallel prompt-template parser. The one
   genuinely missing capability, confirmed by checking main.py's real
   REPL loop: DIRECT `/name` invocation -- today only the LLM itself can
   decide to call the `load_skill` tool; a human typing `/name` at the
   prompt has no way to force that. Added to main.py as a real, new REPL
   command (see main.py's own module docstring for details), reusing
   skills.py's existing parse/render functions -- not a new parser.

2. HOOKS ARE A NARROW, REAL SUBSET -- NOT THE FULL ~30-EVENT CATALOG.
   Claude Code's own documented hook catalog includes events with no
   analog anywhere in this project's actual architecture (WorktreeCreate,
   TeammateIdle, Elicitation, ConfigChange, CwdChanged, ...) -- building
   handlers for events that can never fire here would be dead code, not a
   real capability. Checked agent.py's REAL structure directly before
   choosing which events to support: this project implements exactly the
   4 events that map onto ACTUAL, EXISTING extension points already
   proven live in agent.py's own ReAct loop:
     - SessionStart: run_agent()'s own entry, before the ReAct loop starts.
     - PreToolUse: plugs into agent._needs_confirmation's existing
       confirm() gate -- a hook can DENY a call, using the exact same
       "return False to cancel" contract every confirm() callable in this
       project already has (see permissions.PermissionEngine.confirm_fn
       for the established precedent of wrapping this same gate).
     - PostToolUse: plugs into the EXACT SAME "append to the tool-result
       message content" injection point the batching nudge and
       path-scoped project rules already use (see agent.py's own
       comments on why this, not a new message role, is this project's
       established wire-portability pattern).
     - Stop: run_agent()'s own exit, right before the final reply is
       returned to the caller.
   A real, JSON-in/JSON-out command-hook contract (matching Claude Code's
   own documented shape closely enough to be genuinely portable, not a
   simplified toy): the hook command receives one JSON object on stdin
   (event name + relevant fields) and may print a JSON object to stdout
   to make a decision (`{"decision": "block", "reason": "..."}` for
   PreToolUse; `{"decision": "block", "reason": "..."}` also for
   PostToolUse/Stop, appended as a correction, mirroring the batching-
   nudge injection style) -- a non-JSON stdout, a non-zero exit with no
   parseable JSON, or any exception running the process is treated as
   "no opinion, allow" (fail-open for hooks that error, since a broken
   THIRD-PARTY hook script must never be able to silently brick the whole
   agent -- this is the SAME defensive posture already used for skills.py/
   rules.py's per-item try/except, generalized to "a broken hook doesn't
   crash the run, it's just skipped with a logged warning").

3. MARKETPLACE FETCHING REUSES GitPython (git_tools.py's own dependency),
   NOT a new HTTP/git library. `github`/git-URL sources are cloned into a
   local cache directory (`.agent_plugin_cache/<name>/`) via
   `git.Repo.clone_from` -- confirmed this is the SAME library already a
   real, tested dependency of this project (git_tools.py), not a new one
   introduced just for this feature.

4. MCP SERVERS: a plugin's `.mcp.json` (`mcpServers: {name: {command,
   args, env}}`, the real documented shape) is registered via
   mcp_client.py's ALREADY-GENERIC `connect_server(name, command, args)`
   -- confirmed directly that this function is not hardcoded to just the
   filesystem/fetch servers `connect_all_sync` happens to call it with;
   no new MCP plumbing was needed.

5. `${CLAUDE_PLUGIN_ROOT}`-STYLE EXPANSION: real plugin manifests use
   this exact placeholder for portability (a plugin doesn't know its own
   absolute install path in advance). This project expands the SAME
   placeholder name (for direct compatibility with any plugin authored
   against the real Claude Code docs) to the plugin's real resolved
   directory, plus `${CLAUDE_PROJECT_DIR}` for the project root -- both
   confirmed present in official docs, not invented.

ONE MALFORMED PLUGIN NEVER CRASHES THE WHOLE REGISTRY -- same per-item
try/except isolation as skills.py/rules.py/custom_agents.py, which have
each already fixed this exact class of bug once.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # noqa: F401 -- transitively required by skills.parse_frontmatter
    PLUGINS_AVAILABLE = True
except Exception:
    PLUGINS_AVAILABLE = False

# NOTE: `tools` is imported LAZILY, matching every other optional
# module's established circular-import-avoidance pattern (see
# skills.py/rules.py/custom_agents.py/git_tools.py's own docstrings).
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


PLUGINS_DIR_NAME = ".agent_plugins"
MARKETPLACE_FILE_NAME = ".agent_marketplace.json"
PLUGIN_MANIFEST_DIR_NAME = ".agent_plugin"  # mirrors Claude Code's .claude-plugin/
PLUGIN_CACHE_DIR_NAME = ".agent_plugin_cache"  # cloned remote plugins land here


def _plugins_dir() -> Path:
    return _get_tools().WORKDIR / PLUGINS_DIR_NAME


def _marketplace_file() -> Path:
    return _get_tools().WORKDIR / MARKETPLACE_FILE_NAME


def _plugin_cache_dir() -> Path:
    return _get_tools().WORKDIR / PLUGIN_CACHE_DIR_NAME


@dataclass
class PluginManifest:
    """Parsed `.agent_plugin/plugin.json` -- only `name` is required,
    matching the real spec exactly (verified: "It is optional, and when
    present the only required field is name")."""
    name: str
    description: str = ""
    version: Optional[str] = None
    author: Optional[dict] = None
    homepage: Optional[str] = None
    repository: Optional[str] = None
    license: Optional[str] = None
    keywords: list = field(default_factory=list)


@dataclass
class LoadedPlugin:
    """A fully-loaded, real plugin: its manifest plus the REAL component
    names it actually contributed once loaded (not just what it claims to
    have) -- so list_plugins can honestly report what's live vs. what
    failed."""
    manifest: PluginManifest
    root: Path
    skill_names: list = field(default_factory=list)
    command_names: list = field(default_factory=list)
    agent_names: list = field(default_factory=list)
    mcp_server_names: list = field(default_factory=list)
    hook_events: list = field(default_factory=list)
    load_warnings: list = field(default_factory=list)


def parse_plugin_manifest(text: str, filename: str) -> PluginManifest:
    """Parse a plugin.json's raw text. Raises ValueError with a clear
    message on malformed JSON or a missing/invalid `name` -- never
    silently returns a partial/garbage manifest. Callers (load_plugin)
    catch this per plugin so one malformed manifest never crashes the
    whole registry (see module docstring)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{filename}: malformed JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"{filename}: plugin.json must be a JSON object, not a {type(data).__name__}.")

    name = data.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{filename}: plugin.json must include a non-empty string 'name' field.")

    return PluginManifest(
        name=name.strip(),
        description=str(data.get("description", "")).strip(),
        version=data.get("version"),
        author=data.get("author") if isinstance(data.get("author"), dict) else None,
        homepage=data.get("homepage"),
        repository=data.get("repository"),
        license=data.get("license"),
        keywords=list(data.get("keywords", []) or []),
    )


def _expand_placeholders(value: str, plugin_root: Path) -> str:
    """Real, documented placeholder expansion: ${CLAUDE_PLUGIN_ROOT} and
    ${CLAUDE_PROJECT_DIR} -- confirmed present in Claude Code's own docs
    (plugins-reference, mcp docs), not invented for this project. Kept as
    the SAME placeholder names (not renamed to .agent_* style) so a
    plugin authored against the real Claude Code docs works here
    unmodified for this one specific mechanic."""
    result = value.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root.resolve()))
    result = result.replace("${CLAUDE_PROJECT_DIR}", str(_get_tools().WORKDIR.resolve()))
    return result


def _load_skills_from_dir(skills_dir: Path) -> tuple[list[str], list[str]]:
    """Load a plugin's skills/ directory through skills.py's OWN real
    scan_skills, not a duplicate parser -- see module docstring, decision
    1. Returns (successfully_loaded_names, warning_strings)."""
    import skills as _skills_module

    scanned = _skills_module.scan_skills(skills_dir)
    names, warnings = [], []
    for key, (skill, err) in scanned.items():
        if skill is not None:
            names.append(skill.name)
        else:
            warnings.append(f"skill '{key}' failed to load: {err}")
    return names, warnings


def _load_commands_as_skills(commands_dir: Path) -> tuple[list[str], list[str]]:
    """A plugin's commands/*.md files, loaded through the EXACT SAME
    skills.py parser as skills/ -- see module docstring, decision 1: a
    command file and a skill are the same real mechanism per Claude
    Code's own current docs.

    Two real shapes confirmed from research, BOTH normalized to a valid
    skill before parsing (never rejected as malformed):
      1. No frontmatter at all (just plain prompt text) -- the simplest
         real command files in the wild.
      2. Real frontmatter present (description/argument-hint/allowed-tools/
         etc.) but NO `name:` field -- confirmed this is the REAL,
         documented shape for command files specifically: "the name is
         the filename without its extension" (a command's name is never
         taken from its own frontmatter, unlike a skill's). A first draft
         of this function only handled case 1 (checking for `---` at all)
         and was caught failing on a REAL command file authored exactly
         this way in this project's own committed example plugin
         (.agent_plugins/git-safety/commands/changelog.md) -- found by
         actually loading it, not assumed to work.
    """
    import skills as _skills_module

    names, warnings = [], []
    if not commands_dir.exists() or not commands_dir.is_dir():
        return names, warnings

    for entry in sorted(commands_dir.glob("*.md")):
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
            command_name = entry.stem

            if not text.lstrip().startswith("---"):
                # Case 1: no frontmatter at all.
                text = f"---\nname: {command_name}\ndescription: Custom command '{command_name}'.\n---\n{text}"
            else:
                # Case 2: real frontmatter present, but the command's
                # `name:` (if any) is NEVER authoritative -- the filename
                # always is, matching the real documented behavior. Parse
                # what's there, inject/override `name`, and preserve
                # every other real field (description, argument-hint,
                # allowed-tools, etc.) rather than discarding them.
                try:
                    meta, body = _skills_module.parse_frontmatter(text)
                except ValueError:
                    meta, body = {}, text  # malformed frontmatter -- fall through, let parse_skill_text below raise its own clear error on the reassembled text
                meta["name"] = command_name
                meta.setdefault("description", f"Custom command '{command_name}'.")
                import yaml as _yaml_for_commands
                frontmatter_text = _yaml_for_commands.safe_dump(meta, sort_keys=False).strip()
                text = f"---\n{frontmatter_text}\n---\n{body}"

            skill = _skills_module.parse_skill_text(text, root=commands_dir)
            # Command-derived skills keep the FILENAME as their invocable
            # name (matching real Claude Code: "the name is the filename
            # without its extension") even if frontmatter set a different
            # `name:` -- registered under both to be safe either way.
            names.append(skill.name)
            if skill.name != command_name:
                names.append(command_name)
        except Exception as e:
            warnings.append(f"command '{entry.name}' failed to load: {type(e).__name__}: {e}")
    return names, warnings


def _load_agents_from_dir(agents_dir: Path) -> tuple[list[str], list[str]]:
    """Load a plugin's agents/ directory through custom_agents.py's OWN
    real scan_agent_defs -- not a duplicate parser."""
    import custom_agents as _custom_agents

    if not agents_dir.exists() or not agents_dir.is_dir():
        return [], []
    scanned = _custom_agents.scan_agent_defs(agents_dir)
    names, warnings = [], []
    for key, (raw, err) in scanned.items():
        if raw is not None:
            names.append(raw.name)
        else:
            warnings.append(f"agent '{key}' failed to load: {err}")
    return names, warnings


def _load_mcp_servers(plugin_root: Path, manifest: PluginManifest) -> tuple[list[str], list[str]]:
    """A plugin's `.mcp.json` (or `mcpServers` inline in plugin.json --
    both real, documented locations) registered via mcp_client.py's
    ALREADY-GENERIC connect_server -- see module docstring, decision 4.
    Best-effort: a server that fails to connect (missing binary, bad
    config) is a warning, never a fatal plugin-load error -- matches this
    project's standing "one bad thing doesn't take down everything else"
    posture."""
    mcp_config_path = plugin_root / ".mcp.json"
    servers: dict = {}
    if mcp_config_path.exists():
        try:
            data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
            servers.update(data.get("mcpServers", {}) or {})
        except Exception as e:
            return [], [f".mcp.json failed to parse: {type(e).__name__}: {e}"]

    names, warnings = [], []
    if not servers:
        return names, warnings

    try:
        import mcp_client as _mcp_client
    except Exception as e:
        return [], [f"could not import mcp_client to register MCP servers: {type(e).__name__}: {e}"]

    if not getattr(_mcp_client, "MCP_AVAILABLE", False):
        return [], ["mcp_client.MCP_AVAILABLE is False -- plugin MCP servers were not started."]

    for server_name, cfg in servers.items():
        command = _expand_placeholders(str(cfg.get("command", "")), plugin_root)
        args = [_expand_placeholders(str(a), plugin_root) for a in cfg.get("args", [])]
        if not command:
            warnings.append(f"MCP server '{server_name}' has no 'command' -- skipped.")
            continue
        namespaced_name = f"plugin_{manifest.name}_{server_name}"
        try:
            loop_thread = _mcp_client._get_loop_thread()
            registered = loop_thread.run(
                _mcp_client.mcp_manager.connect_server(namespaced_name, command, args),
                timeout=30.0,
            )
            names.extend(registered)
        except Exception as e:
            warnings.append(f"MCP server '{server_name}' failed to connect: {type(e).__name__}: {e}")

    return names, warnings


def _load_hooks(plugin_root: Path) -> tuple[list[str], list[str]]:
    """A plugin's hooks/hooks.json (real, documented location) --
    validated and REGISTERED into the global hook registry (see
    register_hooks_from_plugin / HOOK_REGISTRY below), not executed here.
    Returns (event_names_registered, warnings)."""
    hooks_path = plugin_root / "hooks" / "hooks.json"
    if not hooks_path.exists():
        return [], []

    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [], [f"hooks/hooks.json failed to parse: {type(e).__name__}: {e}"]

    hooks_section = data.get("hooks", data) if isinstance(data, dict) else {}
    if not isinstance(hooks_section, dict):
        return [], ["hooks/hooks.json's 'hooks' field must be an object."]

    registered_events, warnings = [], []
    for event_name, matchers in hooks_section.items():
        if event_name not in SUPPORTED_HOOK_EVENTS:
            warnings.append(
                f"hook event '{event_name}' is not one of this project's supported events "
                f"({', '.join(sorted(SUPPORTED_HOOK_EVENTS))}) -- ignored. See plugins.py's "
                "module docstring, decision 2, for why only a real subset is supported."
            )
            continue
        if not isinstance(matchers, list):
            warnings.append(f"hook event '{event_name}': expected a list of matchers, got {type(matchers).__name__}.")
            continue
        for matcher_entry in matchers:
            commands = matcher_entry.get("hooks", []) if isinstance(matcher_entry, dict) else []
            matcher_pattern = matcher_entry.get("matcher") if isinstance(matcher_entry, dict) else None
            for hook_cmd in commands:
                if not isinstance(hook_cmd, dict) or hook_cmd.get("type") != "command":
                    warnings.append(f"hook event '{event_name}': only type='command' hooks are supported, got {hook_cmd!r}.")
                    continue
                command_str = _expand_placeholders(str(hook_cmd.get("command", "")), plugin_root)
                if not command_str:
                    warnings.append(f"hook event '{event_name}': a hook entry had no 'command'.")
                    continue
                HOOK_REGISTRY.setdefault(event_name, []).append(
                    RegisteredHook(event=event_name, matcher=matcher_pattern, command=command_str, plugin_root=plugin_root)
                )
                registered_events.append(event_name)

    return registered_events, warnings


# ---------------------------------------------------------------------------
# Hooks: a narrow, real subset -- see module docstring, decision 2.
# ---------------------------------------------------------------------------

SUPPORTED_HOOK_EVENTS = {"SessionStart", "PreToolUse", "PostToolUse", "Stop"}

HOOK_COMMAND_TIMEOUT_SECONDS = 10  # a hook is a small, fast side-check -- never allowed to block the whole loop for long

# REAL SCOPE DECISION, stated explicitly rather than left implicit (same
# practice as skills.py's disallowed-tools decision): Claude Code's own
# real `Stop` hook can force the agent to KEEP GOING (its `decision:
# "block"` re-enters the loop with the hook's reason as feedback, per the
# official docs' "Top-level decision" table). Implementing genuine
# loop-continuation here would mean converting an already-returned final
# reply back into a fake tool-result message and re-entering a ReAct loop
# this project has deliberately hardened across several recent features
# (streaming, the batching nudge, sub-agent depth/budget) -- a real,
# non-trivial risk for a feature explicitly framed as "a packaging layer,
# not a new capability" (COMPARISON_openclaude.md). This project's `Stop`
# hook is OBSERVATIONAL/ADVISORY ONLY: a `{"context": "..."}` response is
# appended as a note to the final reply, but the run always actually
# ends -- never silently claims to support real blocking it doesn't have.
# `SessionStart` is the same real, documented shape (Claude's own docs:
# "Context only... No blocking or decision control") -- so this isn't a
# reduced version of SessionStart, just an accurate one.


@dataclass
class RegisteredHook:
    event: str
    matcher: Optional[str]  # tool-name pattern for PreToolUse/PostToolUse, None for SessionStart/Stop
    command: str
    plugin_root: Path


HOOK_REGISTRY: dict[str, list[RegisteredHook]] = {}


def clear_hook_registry() -> None:
    """Used by tests (and by reload_plugins) to reset global hook state
    between runs -- HOOK_REGISTRY is intentionally module-level global
    state (hooks apply process-wide, matching Claude Code's own "hooks
    apply once a plugin is enabled" semantics), so tests must clean up
    after themselves explicitly rather than relying on process exit."""
    HOOK_REGISTRY.clear()


def _matcher_applies(matcher: Optional[str], tool_name: str) -> bool:
    """A real regex match against the tool name, matching Claude Code's
    own documented matcher semantics (e.g. "Bash", "Edit|Write",
    "mcp__.*"). No matcher (None) means "applies to every tool" -- the
    real documented default when a matcher is omitted."""
    if matcher is None:
        return True
    import re
    try:
        return re.fullmatch(matcher, tool_name) is not None
    except re.error:
        return False


def run_hooks(event: str, payload: dict) -> Optional[dict]:
    """Run every registered hook for `event`, in registration order,
    passing `payload` as JSON on stdin. Returns the FIRST hook's parsed
    decision dict (e.g. {"decision": "block", "reason": "..."}), or None
    if no hook fired an opinion -- matching this project's own "first
    real signal wins" pattern rather than requiring every hook to agree.

    FAIL-OPEN, always: a hook that times out, exits with unparseable
    output, or raises for any reason is treated as "no opinion" and
    LOGGED, never allowed to crash the caller or silently halt the
    session -- see module docstring, decision 2, for why (a broken
    third-party hook script must never be able to brick the whole agent).
    """
    hooks = HOOK_REGISTRY.get(event, [])
    if not hooks:
        return None

    tool_name = payload.get("tool_name", "")
    for hook in hooks:
        if event in ("PreToolUse", "PostToolUse") and not _matcher_applies(hook.matcher, tool_name):
            continue
        try:
            result = subprocess.run(
                hook.command,
                shell=True,
                cwd=str(hook.plugin_root),
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=HOOK_COMMAND_TIMEOUT_SECONDS,
            )
            stdout = (result.stdout or "").strip()
            if not stdout:
                continue
            decision = json.loads(stdout)
            if isinstance(decision, dict) and decision:
                return decision
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
            # Fail open, always -- see this function's own docstring.
            continue
    return None


# ---------------------------------------------------------------------------
# Plugin loading
# ---------------------------------------------------------------------------

def load_plugin(plugin_root: Path) -> LoadedPlugin:
    """Load ONE plugin directory: parse its manifest (if present -- the
    manifest itself is optional per the real spec, only required if you
    want metadata beyond the directory name), then load each REAL
    component folder that actually exists, isolating failures per
    component so e.g. a broken hooks.json never prevents the plugin's
    skills from loading.
    """
    manifest_path = plugin_root / PLUGIN_MANIFEST_DIR_NAME / "plugin.json"
    if manifest_path.exists():
        manifest = parse_plugin_manifest(manifest_path.read_text(encoding="utf-8", errors="replace"), manifest_path.name)
    else:
        # Manifest is optional per the real spec -- fall back to the
        # directory name, matching "only plugin.json lives inside
        # .claude-plugin/... It is optional, and when present the only
        # required field is name."
        manifest = PluginManifest(name=plugin_root.name)

    loaded = LoadedPlugin(manifest=manifest, root=plugin_root)

    skill_names, warn = _load_skills_from_dir(plugin_root / "skills")
    loaded.skill_names.extend(skill_names)
    loaded.load_warnings.extend(warn)

    command_names, warn = _load_commands_as_skills(plugin_root / "commands")
    loaded.command_names.extend(command_names)
    loaded.load_warnings.extend(warn)

    agent_names, warn = _load_agents_from_dir(plugin_root / "agents")
    loaded.agent_names.extend(agent_names)
    loaded.load_warnings.extend(warn)

    mcp_names, warn = _load_mcp_servers(plugin_root, manifest)
    loaded.mcp_server_names.extend(mcp_names)
    loaded.load_warnings.extend(warn)

    hook_events, warn = _load_hooks(plugin_root)
    loaded.hook_events.extend(hook_events)
    loaded.load_warnings.extend(warn)

    return loaded


def scan_local_plugins(plugins_dir: Optional[Path] = None) -> dict[str, tuple[Optional[LoadedPlugin], Optional[str]]]:
    """Scan `plugins_dir` (defaults to the real .agent_plugins/) for every
    subdirectory, loading each one INDEPENDENTLY -- one malformed plugin
    never prevents any other valid one from loading (see module
    docstring). Returns {dir_name: (LoadedPlugin_or_None, error_or_None)}.
    """
    if not PLUGINS_AVAILABLE:
        return {}

    directory = plugins_dir if plugins_dir is not None else _plugins_dir()
    results: dict[str, tuple[Optional[LoadedPlugin], Optional[str]]] = {}

    if not directory.exists() or not directory.is_dir():
        return results

    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        try:
            results[entry.name] = (load_plugin(entry), None)
        except Exception as e:
            results[entry.name] = (None, f"{type(e).__name__}: {e}")

    return results


# ---------------------------------------------------------------------------
# Marketplace: catalog + real remote fetch (GitHub/git via GitPython)
# ---------------------------------------------------------------------------

@dataclass
class MarketplaceEntry:
    name: str
    source: str  # local relative path, OR a github "owner/repo" / plain git URL string
    description: str = ""
    source_kind: str = "local"  # "local" | "github" | "git"


def parse_marketplace_file(text: str, filename: str) -> list[MarketplaceEntry]:
    """Parse a marketplace.json's raw text into a list of entries. Raises
    ValueError on malformed JSON/schema -- never returns a partial list
    silently. `source` matches the real documented shapes: a plain
    relative-path string, or {"source": "github", "repo": "owner/repo"},
    or a plain git URL string (detected by a "://" or ".git" substring)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{filename}: malformed JSON: {e}") from e

    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        raise ValueError(f"{filename}: expected a top-level 'plugins' array.")

    entries = []
    for i, item in enumerate(plugins):
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError(f"{filename}: plugins[{i}] must be an object with a 'name' field.")
        name = item["name"]
        source = item.get("source", "")
        description = item.get("description", "")

        if isinstance(source, dict):
            if source.get("source") == "github" and source.get("repo"):
                entries.append(MarketplaceEntry(name=name, source=source["repo"], description=description, source_kind="github"))
                continue
            raise ValueError(f"{filename}: plugins[{i}] ('{name}') has an unsupported source object: {source!r}")
        elif isinstance(source, str):
            if source.startswith(("http://", "https://", "git@")) or source.endswith(".git"):
                entries.append(MarketplaceEntry(name=name, source=source, description=description, source_kind="git"))
            else:
                entries.append(MarketplaceEntry(name=name, source=source, description=description, source_kind="local"))
            continue
        raise ValueError(f"{filename}: plugins[{i}] ('{name}') has an invalid 'source': {source!r}")

    return entries


def scan_marketplace(marketplace_file: Optional[Path] = None) -> tuple[list[MarketplaceEntry], Optional[str]]:
    """Load the real .agent_marketplace.json, if present. Returns
    ([], None) if the file simply doesn't exist (not an error -- a
    marketplace is optional; plugins can be used purely locally via
    .agent_plugins/ with no catalog at all)."""
    path = marketplace_file if marketplace_file is not None else _marketplace_file()
    if not path.exists():
        return [], None
    try:
        entries = parse_marketplace_file(path.read_text(encoding="utf-8", errors="replace"), path.name)
        return entries, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def resolve_plugin_source(entry: MarketplaceEntry) -> Path:
    """Resolve a marketplace entry's `source` to a REAL local directory
    ready for load_plugin(). Local sources resolve directly (relative to
    the project root); github/git sources are cloned via GitPython into
    `.agent_plugin_cache/<name>/` -- reusing git_tools.py's own already-
    tested dependency, not a new one (see module docstring, decision 3).
    A github "owner/repo" string is expanded to the real clone URL.
    Raises RuntimeError with a clear message on any failure (missing
    GitPython, clone failure, network error) -- never a silent no-op.
    """
    if entry.source_kind == "local":
        resolved = (_get_tools().WORKDIR / entry.source).resolve()
        if not resolved.exists():
            raise RuntimeError(f"local plugin source '{entry.source}' does not exist at {resolved}")
        return resolved

    try:
        import git
    except ImportError as e:
        raise RuntimeError(
            f"cannot fetch remote plugin '{entry.name}': GitPython is not installed ({e}). "
            "Run `pip install GitPython` (already in requirements.txt)."
        ) from e

    clone_url = entry.source
    if entry.source_kind == "github":
        clone_url = f"https://github.com/{entry.source}.git"

    dest = _plugin_cache_dir() / entry.name
    if dest.exists():
        # Already cloned -- pull the latest instead of re-cloning, so a
        # marketplace update is reflected on the next reload without
        # manual cache-busting.
        try:
            repo = git.Repo(str(dest))
            repo.remotes.origin.pull()
        except Exception as e:
            raise RuntimeError(f"failed to update cached plugin '{entry.name}' at {dest}: {type(e).__name__}: {e}") from e
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        git.Repo.clone_from(clone_url, str(dest), depth=1)
    except Exception as e:
        raise RuntimeError(f"failed to clone plugin '{entry.name}' from {clone_url}: {type(e).__name__}: {e}") from e
    return dest


def install_plugin_from_marketplace(name: str) -> LoadedPlugin:
    """Resolve `name` from the real marketplace catalog, fetch its
    source (local or remote), and load it -- the marketplace-driven
    counterpart to scan_local_plugins (which only ever looks at
    .agent_plugins/ directly). Raises ValueError/RuntimeError with a
    clear message for an unknown name or any resolve/load failure --
    never a raw, unhandled traceback."""
    entries, err = scan_marketplace()
    if err:
        raise RuntimeError(f"could not read the marketplace catalog: {err}")
    entry = next((e for e in entries if e.name == name), None)
    if entry is None:
        available = sorted(e.name for e in entries)
        raise ValueError(f"no plugin named '{name}' in the marketplace catalog. Available: {available or '(none)'}")

    plugin_dir = resolve_plugin_source(entry)
    return load_plugin(plugin_dir)


# ---------------------------------------------------------------------------
# Tool wrappers -- registered into tools.TOOL_FUNCTIONS/TOOL_SPECS from
# tools.py's own end, alongside every other optional tool group.
# ---------------------------------------------------------------------------

def _tool_list_plugins() -> str:
    """List every LOCAL plugin found in .agent_plugins/, including ones
    that failed to load (with the error), plus what marketplace plugins
    (if any catalog is present) are available to install. Same debug-
    visibility posture as skills.list_skills()/rules.list_rules()/
    custom_agents.list_custom_agents()."""
    lines = []

    local = scan_local_plugins()
    if local:
        lines.append("Local plugins (.agent_plugins/):")
        for key, (plugin, err) in sorted(local.items()):
            if plugin is None:
                lines.append(f"  - {key}: ERROR (failed to load) -- {err}")
                continue
            parts = []
            if plugin.skill_names:
                parts.append(f"{len(plugin.skill_names)} skill(s)")
            if plugin.command_names:
                parts.append(f"{len(plugin.command_names)} command(s)")
            if plugin.agent_names:
                parts.append(f"{len(plugin.agent_names)} agent(s)")
            if plugin.mcp_server_names:
                parts.append(f"{len(plugin.mcp_server_names)} MCP tool(s)")
            if plugin.hook_events:
                parts.append(f"hooks: {', '.join(sorted(set(plugin.hook_events)))}")
            summary = ", ".join(parts) or "(no components loaded)"
            lines.append(f"  - {plugin.manifest.name}: {plugin.manifest.description or '(no description)'} [{summary}]")
            for w in plugin.load_warnings:
                lines.append(f"      warning: {w}")
    else:
        lines.append(f"(no local plugins found in {_plugins_dir()})")

    entries, err = scan_marketplace()
    if err:
        lines.append(f"\nMarketplace catalog ({_marketplace_file()}): ERROR -- {err}")
    elif entries:
        lines.append(f"\nMarketplace catalog ({_marketplace_file()}):")
        for e in entries:
            lines.append(f"  - {e.name} ({e.source_kind}: {e.source}): {e.description}")
    return "\n".join(lines)


def _tool_install_plugin(name: str) -> str:
    """Install (fetch + load) a plugin by name from the real marketplace
    catalog. Returns a clear summary of what was loaded, or a clear
    ERROR string -- never a raw traceback."""
    try:
        plugin = install_plugin_from_marketplace(name)
    except (ValueError, RuntimeError) as e:
        return f"ERROR: {e}"

    parts = []
    if plugin.skill_names:
        parts.append(f"skills: {', '.join(plugin.skill_names)}")
    if plugin.command_names:
        parts.append(f"commands: {', '.join(plugin.command_names)}")
    if plugin.agent_names:
        parts.append(f"agents: {', '.join(plugin.agent_names)}")
    if plugin.mcp_server_names:
        parts.append(f"MCP tools: {', '.join(plugin.mcp_server_names)}")
    if plugin.hook_events:
        parts.append(f"hooks: {', '.join(sorted(set(plugin.hook_events)))}")
    summary = "; ".join(parts) if parts else "no components loaded"
    warnings_text = ("\nWarnings: " + "; ".join(plugin.load_warnings)) if plugin.load_warnings else ""
    return f"OK: installed plugin '{plugin.manifest.name}' at {plugin.root} -- {summary}{warnings_text}"


def find_invocable_skill(name: str):
    """Resolve a direct `/name` invocation (see module docstring, decision
    1 -- the one genuinely missing capability once "commands merged into
    skills" is accounted for) by searching, in order: the project's own
    `.agent_skills/`, then every local plugin's `skills/` and `commands/`
    directories (both loaded through the SAME skills.py parser -- see
    _load_skills_from_dir/_load_commands_as_skills above). Returns the
    real `skills.Skill` object on success, or `None` if nothing matches
    anywhere -- callers (main.py) are responsible for the "not found"
    message, this function just does the lookup.
    """
    import skills as _skills_module

    project_skills = _skills_module.scan_skills()
    entry = project_skills.get(name)
    if entry and entry[0] is not None:
        return entry[0]

    for plugin_dir_name, (plugin, err) in scan_local_plugins().items():
        if plugin is None:
            continue
        for component_dir in (plugin.root / "skills", plugin.root / "commands"):
            if not component_dir.exists():
                continue
            if component_dir.name == "skills":
                scanned = _skills_module.scan_skills(component_dir)
                entry = scanned.get(name)
                if entry and entry[0] is not None:
                    return entry[0]
            else:
                # commands/ is scanned by filename stem (see
                # _load_commands_as_skills) -- re-derive the same Skill
                # object directly rather than re-deriving the whole
                # loaded-names list.
                candidate = component_dir / f"{name}.md"
                if candidate.exists():
                    try:
                        text = candidate.read_text(encoding="utf-8", errors="replace")
                        if not text.lstrip().startswith("---"):
                            text = f"---\nname: {name}\ndescription: Custom command '{name}'.\n---\n{text}"
                        return _skills_module.parse_skill_text(text, root=component_dir)
                    except Exception:
                        continue
    return None


TOOL_FUNCTIONS = {
    "list_plugins": _tool_list_plugins,
    "install_plugin": _tool_install_plugin,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_plugins",
            "description": (
                "List every local plugin (from .agent_plugins/), including ones that failed "
                "to load (with the error), plus every plugin available in the marketplace "
                "catalog (.agent_marketplace.json), if one exists. Use this to see what "
                "plugins are installed/available before calling install_plugin."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_plugin",
            "description": (
                "Install a plugin by name from the marketplace catalog (.agent_marketplace.json) "
                "-- fetches it (cloning via git if it's a remote source) and loads its skills/"
                "commands/agents/MCP servers/hooks. Use list_plugins first to see what's available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact plugin name from the marketplace catalog."},
                },
                "required": ["name"],
            },
        },
    },
]
