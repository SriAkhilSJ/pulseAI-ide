"""
custom_agents.py
-----------------
Custom agent definitions -- user-authored, version-controlled `.md` files
under `.agent_agents/` that pre-configure a `dispatch_agent` call: a fixed
system prompt, an optional restricted tool set, an optional permission
mode, and optional single-parent inheritance for composing a specialized
agent out of a shared base.

Modeled on the real, currently-documented shape of Claude Code's own
custom subagents (`.claude/agents/*.md`, YAML frontmatter + Markdown
body -- `name`/`description`/`tools`/`model`/`permissionMode` fields,
verified this session against multiple independently-published, current
explainers of Claude Code's real behavior, none of them
Gitlawb/openclaude's leaked `src/`) -- built CLEAN-ROOM against this
project's OWN already-tested subagents.py/permissions.py/skills.py
plumbing, not copied from any leaked source.

WHY A SEPARATE MODULE, NOT A BRANCH INSIDE subagents.py: this module's
job is "parse .md files on disk into a resolved config object with
inheritance flattened" -- a genuinely different responsibility from
subagents.py's "run a restricted ReAct sub-loop," matching this project's
established one-module-per-concern split (skills.py parses SKILL.md,
rules.py parses rule .md files, subagents.py runs the loop; none of them
do each other's job). subagents.py's dispatch_agent calls INTO this
module (see its own updated docstring) exactly the same way agent.py
calls into skills.py/rules.py for system-prompt injection.

DESIGN DECISIONS, made explicitly rather than left ambiguous (a real
proposal for this feature left several open -- resolved here per this
project's own "build the cheap thing first, verify, escalate only if
proven insufficient" rule, since cheap and consistent-with-what's-already-
shipped happened to agree in both cases):

1. NO `model:` FIELD IN V1. Checked directly: llm_client.chat_completion()
   hardcodes `model=first_model` (the Router's FIRST configured deployment
   name) at its own call site inside _run_with_deadline -- there is no
   existing parameter to route a single call to a specific Router
   deployment from outside chat_completion() without either (a) bypassing
   the Router/timeout/fallback wrapper entirely for that one call (losing
   the exact rate-limit-cooldown safety net this whole project's provider
   stack depends on), or (b) adding a new pass-through parameter into
   chat_completion -> agent.run_agent's LLM-call site, a real change to
   the one code path every single call in the project goes through, for a
   feature nobody has a proven need for yet. Recognized rather than
   silently ignored: a `model:` field present in an agent file's
   frontmatter is surfaced as a one-time WARNING in the parse result (not
   a hard error -- the rest of the agent still loads and runs), not
   silently dropped without a trace and not treated as a fatal error for
   an otherwise-valid file.

2. `skills:` IS METADATA-ONLY, NEVER FORCE-PRELOADED. Matches skills.py's
   own already-shipped, explicitly-decided design (see that module's
   docstring, decision 1): Layer 1 (name+description) is cheap and always
   shown; Layer 2 (full body) is loaded only when the model itself calls
   load_skill(name). A custom agent's `skills:` list is folded into the
   sub-agent's own system prompt as the SAME "Available Skills" metadata
   block skills.get_metadata_block() already produces -- filtered down to
   just the named skills (falling back to a clear inline note if a named
   skill doesn't exist, never silently vanishing) -- so the sub-agent
   still has to actively call load_skill to get the real instructions,
   exactly like the main agent does today. This is also the cheaper
   option: force-preloading full skill bodies on every dispatch costs
   tokens on every single call whether or not the task ends up needing
   them.

3. `tools:` COMPOSES WITH `mode:` VIA INTERSECTION, NEVER UNION. If an
   agent sets `mode: plan` (whose own PermissionEngine.restricted_registry
   already returns a read-only-only registry) AND ALSO `tools: [write_file]`,
   the resolved registry is the SUBAGENT_TYPE/mode's read-only set
   INTERSECTED with the requested tools -- `write_file` is silently
   absent from the final registry, never re-granted. This matches every
   other structural-restriction decision already made in this codebase
   (skills.py's tools_hint is advisory-only specifically BECAUSE real
   enforcement would need loop-mutable tool state; permission modes'
   registries are never re-unioned with anything after the fact) -- a
   named agent's `tools:` list can only ever narrow what a mode already
   restricted, never widen it back. Verified with a real test using an
   agent that sets mode=plan and tools=[write_file] together.

4. SINGLE-PARENT INHERITANCE ONLY (`extends: <name>`), not multiple
   inheritance -- keeps composition resolution a straight linear walk
   (child -> parent -> grandparent -> ...) instead of a diamond-inheritance
   merge-order problem no one asked for. Cycle detection: a linear walk
   with a visited-name set; hitting an already-visited name raises
   ValueError with the full cycle shown, same fail-loud-with-a-clear-
   message posture as skills.py's parse_skill_text/rules.py's
   parse_rule_text (never silently accept a malformed definition).

5. FIELD-LEVEL COMPOSITION RULES (child always wins for scalars, resolved
   bottom-up from the root ancestor to the child so a child's own value
   always overrides an inherited one, never the reverse):
     - name/description : always the CHILD's own (never inherited --
       every agent file, including a child, needs its own real name to
       be addressable by `agent_name=` at dispatch time).
     - skills            : UNION, deduplicated, child ∪ parent ∪ grandparent...
     - tools             : REPLACE -- child's own list wins if given at
                            all; if the child omits `tools:` entirely,
                            inherit the NEAREST ancestor that specifies
                            one; if NONE in the chain specify it, None
                            (full registry, subject to mode's own
                            intersection per decision 3).
     - mode              : REPLACE -- same nearest-ancestor-wins rule as
                            tools.
     - max_iterations    : REPLACE -- same nearest-ancestor-wins rule.
     - body              : PREPEND -- child body first, then a `---`
                            separator, then the parent's OWN resolved
                            body (so a 3-level chain shows child, then
                            middle, then root, in that reading order --
                            the child's specialization is what a human/
                            model reads FIRST, with the shared base
                            instructions following for context).

6. `.agent_agents/` is NOT gitignored -- same reasoning as `.agent_skills/`
   and `.agent_rules/`: user-authored content meant to be committed and
   shared across a team, not regenerated runtime state.

ONE MALFORMED AGENT FILE NEVER CRASHES THE WHOLE REGISTRY -- same
per-item try/except isolation as skills.py's scan_skills()/rules.py's
scan_rules(), which both fixed this exact class of bug before shipping
(a single bad frontmatter block used to be able to take down every OTHER
valid file in the same scan).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # noqa: F401 -- only used transitively, via skills.parse_frontmatter
    CUSTOM_AGENTS_AVAILABLE = True
except Exception:
    CUSTOM_AGENTS_AVAILABLE = False

# NOTE: `tools` is imported LAZILY (inside _get_tools()), matching every
# other optional module's own established circular-import-avoidance
# pattern (skills.py/rules.py/subagents.py/git_tools.py/rag_indexer.py) --
# tools.py imports this module (indirectly, via subagents.py) at its own
# end, so a module-level `import tools` here would risk the same silent
# partial-load failure those modules' docstrings already document and
# fixed.
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


AGENTS_DIR_NAME = ".agent_agents"


def _agents_dir() -> Path:
    """Resolved lazily against tools.WORKDIR, same pattern as
    skills._skills_dir()/rules -- so a test can point this at an isolated
    directory without needing tools.py already imported."""
    return _get_tools().WORKDIR / AGENTS_DIR_NAME


@dataclass
class RawAgentDef:
    """One `.md` file's OWN fields, before inheritance is resolved --
    `None` for any field the file didn't specify at all (distinct from an
    explicit empty list/string), so the resolver can tell "not given,
    inherit" apart from "given as empty."""
    name: str
    description: str
    extends: Optional[str] = None
    skills: Optional[list[str]] = None
    tools: Optional[list[str]] = None
    mode: Optional[str] = None
    max_iterations: Optional[int] = None
    body: str = ""
    unknown_fields: list[str] = field(default_factory=list)


@dataclass
class ResolvedAgent:
    """The fully-flattened result of walking `extends` from an ancestor
    down to `name`, per the composition rules in this module's docstring.
    This is what subagents.dispatch_agent actually consumes."""
    name: str
    description: str
    skills: list[str]
    tools: Optional[list[str]]   # None means "full registry" (no ancestor in the chain specified any)
    mode: Optional[str]          # None means "no mode override" (subagents.py's existing behavior)
    max_iterations: Optional[int]
    body: str
    unknown_fields: list[str]    # union across the whole chain, for a clear one-time warning


def parse_agent_text(text: str, filename: str) -> RawAgentDef:
    """
    Parse one agent `.md` file's raw text into a RawAgentDef. Raises
    ValueError with a clear, specific message on malformed input (missing
    frontmatter delimiter, malformed YAML, missing required `name`/
    `description`) -- never silently returns a partial/garbage result.
    Callers (scan_agents) are responsible for catching this per file so
    one bad definition doesn't take down the whole registry, same
    separation of concerns as skills.parse_skill_text/rules.parse_rule_text.

    Reuses skills.parse_frontmatter (the same shared primitive rules.py
    already reuses) rather than reimplementing YAML-frontmatter splitting
    a third time.
    """
    import skills as _skills_module  # local import: avoid a module-load-order dependency on skills.py

    meta, body = _skills_module.parse_frontmatter(text)

    name = meta.get("name")
    description = meta.get("description")
    if not name or not isinstance(name, str):
        raise ValueError(f"{filename}: frontmatter must include a non-empty string 'name' field.")
    if not description or not isinstance(description, str):
        raise ValueError(f"{filename}: frontmatter must include a non-empty string 'description' field.")

    extends = meta.get("extends")
    if extends is not None and not isinstance(extends, str):
        raise ValueError(f"{filename}: 'extends' must be a string agent name, got {type(extends).__name__}.")

    def _as_str_list(value, field_name: str) -> Optional[list[str]]:
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                raise ValueError(f"{filename}: '{field_name}' list must contain only strings.")
            return list(value)
        raise ValueError(f"{filename}: '{field_name}' must be a string or list of strings, got {type(value).__name__}.")

    skills_field = _as_str_list(meta.get("skills"), "skills")
    tools_field = _as_str_list(meta.get("tools"), "tools")

    mode = meta.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise ValueError(f"{filename}: 'mode' must be a string, got {type(mode).__name__}.")

    max_iterations = meta.get("max_iterations")
    if max_iterations is not None and not isinstance(max_iterations, int):
        raise ValueError(f"{filename}: 'max_iterations' must be an integer, got {type(max_iterations).__name__}.")

    # Recognized fields per this module's docstring -- anything else
    # (including a `model:` field, deliberately unsupported in v1, see
    # decision 1) is surfaced as a warning, not a hard error: an
    # otherwise-valid agent file should still load and work.
    _RECOGNIZED = {"name", "description", "extends", "skills", "tools", "mode", "max_iterations"}
    unknown_fields = sorted(set(meta.keys()) - _RECOGNIZED)

    return RawAgentDef(
        name=name.strip(),
        description=description.strip(),
        extends=extends.strip() if extends else None,
        skills=skills_field,
        tools=tools_field,
        mode=mode.strip() if mode else None,
        max_iterations=max_iterations,
        body=body,
        unknown_fields=unknown_fields,
    )


def scan_agent_defs(agents_dir: Optional[Path] = None) -> dict[str, tuple[Optional[RawAgentDef], Optional[str]]]:
    """
    Scan `agents_dir` (defaults to the real .agent_agents/) for every
    `*.md` file, parsing each one INDEPENDENTLY -- a malformed agent file
    never prevents any other valid one from loading, same isolation
    guarantee as skills.scan_skills()/rules.scan_rules().

    Returns {name_from_frontmatter: (RawAgentDef_or_None, error_or_None)}.
    Keyed by the file's OWN declared `name` field (not its filename),
    matching how `extends:`/`agent_name=` reference agents -- a naming
    collision between two files (same `name:`, different filenames) is
    reported as a parse error for the second one encountered (alphabetical
    order), not a silent overwrite.
    """
    if not CUSTOM_AGENTS_AVAILABLE:
        return {}

    directory = agents_dir if agents_dir is not None else _agents_dir()
    results: dict[str, tuple[Optional[RawAgentDef], Optional[str]]] = {}

    if not directory.exists() or not directory.is_dir():
        return results

    for entry in sorted(directory.glob("*.md")):
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
            raw = parse_agent_text(text, filename=entry.name)
        except Exception as e:
            # Per-file isolation: keyed by filename (stem) when parsing
            # failed before we ever learned the file's real `name:`.
            results[entry.stem] = (None, f"{type(e).__name__}: {e}")
            continue
        if raw.name in results and results[raw.name][0] is not None:
            results[raw.name] = (
                None,
                f"ValueError: duplicate agent name '{raw.name}' -- also defined in "
                "an earlier file (alphabetically). Each agent needs a unique 'name'.",
            )
            continue
        results[raw.name] = (raw, None)

    return results


def resolve_agent(name: str, raw_defs: dict[str, tuple[Optional[RawAgentDef], Optional[str]]]) -> ResolvedAgent:
    """
    Flatten `name`'s full `extends` chain into one ResolvedAgent, per the
    field-level composition rules in this module's docstring (decision 5).

    Raises ValueError (never silently returns a partial result) for:
      - `name` not found in raw_defs at all.
      - `name` found but failed to parse (the stored error is included).
      - a broken link: `extends: X` where X isn't a valid, successfully-
        parsed agent.
      - a cycle: A extends B extends ... extends A. The full chain is
        included in the error message so it's actually debuggable, not
        just "cycle detected."
    """
    chain: list[RawAgentDef] = []
    visited: list[str] = []
    current = name

    while True:
        if current in visited:
            cycle_display = " -> ".join(visited + [current])
            raise ValueError(f"cycle detected in agent inheritance: {cycle_display}")
        visited.append(current)

        entry = raw_defs.get(current)
        if entry is None:
            if current == name:
                raise ValueError(f"no agent named '{name}' found.")
            raise ValueError(
                f"agent '{visited[-2]}' extends '{current}', which does not exist."
            )
        raw, err = entry
        if raw is None:
            raise ValueError(f"agent '{current}' failed to parse: {err}")

        chain.append(raw)
        if raw.extends is None:
            break
        current = raw.extends

    # chain is [child, parent, grandparent, ..., root]. Walk it in
    # REVERSE (root first) so a child's own value always overrides an
    # already-set inherited one, per decision 5's "nearest-ancestor-wins,
    # child always wins over anything inherited" rule.
    root_to_child = list(reversed(chain))
    child = chain[0]  # the originally-requested agent -- always wins for name/description

    skills_union: list[str] = []
    tools_value: Optional[list[str]] = None
    mode_value: Optional[str] = None
    max_iterations_value: Optional[int] = None
    unknown_fields_union: set[str] = set()

    for raw in root_to_child:
        if raw.skills:
            for s in raw.skills:
                if s not in skills_union:
                    skills_union.append(s)
        if raw.tools is not None:
            tools_value = raw.tools
        if raw.mode is not None:
            mode_value = raw.mode
        if raw.max_iterations is not None:
            max_iterations_value = raw.max_iterations
        unknown_fields_union.update(raw.unknown_fields)

    # body: PREPEND child's own body, then parent's own, then
    # grandparent's, etc. -- chain is already [child, parent, ...] in
    # that exact order, so a plain join reproduces decision 5's rule
    # directly without needing to re-reverse anything.
    body_parts = [raw.body for raw in chain if raw.body.strip()]
    resolved_body = "\n\n---\n\n".join(body_parts)

    return ResolvedAgent(
        name=child.name,
        description=child.description,
        skills=skills_union,
        tools=tools_value,
        mode=mode_value,
        max_iterations=max_iterations_value,
        body=resolved_body,
        unknown_fields=sorted(unknown_fields_union),
    )


def skills_metadata_block_for(skill_names: list[str]) -> str:
    """Build the SAME 'Available Skills' metadata block skills.py's
    get_metadata_block produces for the main agent, but filtered down to
    only the names this custom agent listed -- per decision 2 (metadata-
    only, never force-preloaded; the sub-agent still has to call
    load_skill itself). A name that doesn't correspond to any real,
    successfully-parsed skill is surfaced as an inline note (never
    silently dropped without a trace), matching this project's standing
    "never fail silently" posture."""
    if not skill_names:
        return ""
    if not _get_tools().SKILLS_AVAILABLE:
        return (
            f"(This agent lists skills ({', '.join(skill_names)}) but skills.py's "
            "dependency (PyYAML) isn't available -- skills cannot be loaded this run.)"
        )
    import skills as _skills_module

    scanned = _skills_module.scan_skills()
    lines = ["Available Skills (call load_skill(name) when relevant):"]
    missing = []
    for wanted_name in skill_names:
        entry = scanned.get(wanted_name)
        if entry is None or entry[0] is None:
            missing.append(wanted_name)
            continue
        skill = entry[0]
        lines.append(f"- {skill.name}: {skill.description}")
    if missing:
        lines.append(f"(Note: this agent lists skill(s) not found or failed to load: {', '.join(missing)})")
    if len(lines) == 1 and missing:
        # every listed skill was missing -- still return the note, not an empty string,
        # so the gap is visible rather than silently vanishing.
        return "\n".join(lines)
    return "\n".join(lines)


def build_agent_system_prompt(resolved: ResolvedAgent, tool_list: str) -> str:
    """The sub-agent's system prompt for a NAMED custom agent dispatch:
    resolved.body (the agent's own instructions, already inheritance-
    flattened) plus a skills metadata block (decision 2) plus the same
    tool-list framing subagents._SUBAGENT_SYSTEM_PROMPT_TEMPLATE already
    uses for subagent_type dispatches, so a named-agent dispatch and a
    subagent_type dispatch read consistently to the model."""
    parts = [resolved.body.strip()] if resolved.body.strip() else []
    parts.append(f"\nYour available tools: {tool_list}")
    skills_block = skills_metadata_block_for(resolved.skills)
    if skills_block:
        parts.append(f"\n{skills_block}")
    if resolved.unknown_fields:
        parts.append(
            f"\n(Note: this agent definition had unrecognized frontmatter field(s) "
            f"{', '.join(resolved.unknown_fields)} -- ignored; see custom_agents.py's "
            "module docstring, decision 1, e.g. 'model:' is not yet supported.)"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool wrapper -- registered into tools.TOOL_FUNCTIONS/TOOL_SPECS from
# tools.py's own end, alongside every other optional tool group, exactly
# like skills.py/rules.py's own tools are.
# ---------------------------------------------------------------------------

def _tool_list_agents() -> str:
    """List every custom agent definition found, including ones that
    failed to parse or resolve (with the error) -- same debug-visibility
    posture as skills.list_skills()/rules.list_rules()."""
    raw_defs = scan_agent_defs()
    if not raw_defs:
        return f"(no custom agent definitions found in {_agents_dir()})"
    lines = []
    for key, (raw, err) in sorted(raw_defs.items()):
        if raw is None:
            lines.append(f"- {key}: ERROR (failed to load) -- {err}")
            continue
        try:
            resolved = resolve_agent(raw.name, raw_defs)
            extends_note = f" (extends: {raw.extends})" if raw.extends else ""
            lines.append(f"- {resolved.name}: {resolved.description}{extends_note}")
        except ValueError as e:
            lines.append(f"- {raw.name}: ERROR (failed to resolve inheritance) -- {e}")
    return "\n".join(lines)


TOOL_FUNCTIONS = {
    "list_custom_agents": _tool_list_agents,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_custom_agents",
            "description": (
                "List every custom agent definition found in .agent_agents/, including "
                "ones that failed to parse or resolve (with the error) -- use this to see "
                "what named agents are available to pass as dispatch_agent's agent_name "
                "parameter, or to debug why an expected one isn't showing up."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
