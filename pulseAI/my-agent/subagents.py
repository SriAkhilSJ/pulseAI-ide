"""
subagents.py
------------
"dispatch_agent" -- a tool that lets the main agent delegate a self-
contained sub-task to a SEPARATE, isolated ReAct loop (its own
run_agent() call, its own fresh conversation history, its own restricted
tool set), returning exactly ONE final summary string back to the caller
instead of every intermediate Thought/Action/Observation.

Modeled on the publicly-documented shape of Claude Code's own `Task` tool
(schema: description/prompt/subagent_type/model/resume/run_in_background --
verified this session against independently-published explainers that
quote the tool's own JSON schema, not extracted from any leaked source)
and OpenClaude's own published `maxSteps` cap concept -- built CLEAN-ROOM
against this project's OWN already-tested agent.py plumbing, not copied
from either.

WHY A SEPARATE MODULE, NOT A NEW BRANCH INSIDE tools.py: the restriction
mechanism (see subagent_type -> allowed tool names below) needs its own
namespace of "how do I build a restricted registry" logic that would
clutter tools.py's existing "register real tools" role. tools.py still
owns the actual TOOL_FUNCTIONS/TOOL_SPECS registration for dispatch_agent
itself (added at the very end of tools.py, alongside every other optional
tool group), exactly like git_tools.py/rag_indexer.py/ast_tools.py do.

REAL, ENFORCED restriction (not a prompt-level suggestion): a sub-agent
scoped to "explore" is handed a `tool_functions` dict that STRUCTURALLY
DOES NOT CONTAIN "write_file"/"apply_edit"/"run_command"/etc. at all (see
agent._dispatch_tool_call's `tool_functions` parameter, added specifically
to support this). If the sub-agent's own LLM call somehow still emits a
tool_call naming "write_file", _dispatch_tool_call looks it up in the
RESTRICTED dict, doesn't find it, and returns
"ERROR: unknown tool 'write_file'" -- the exact same failure mode as a
genuinely nonexistent tool name, not a permission check that could be
reasoned around. `subagent_type`'s own TOOL_SPECS (what the sub-agent's
model is even told exists) is ALSO restricted, so the model is never even
prompted with tools it structurally cannot call.

COST / RATE-LIMIT SAFETY (the user's own concern, confirmed this session):
each dispatch_agent call is a COMPLETE separate multi-turn ReAct loop, not
a single extra LLM call -- Anthropic's own published guidance says multi-
agent workflows use roughly 4-7x the tokens of a single-agent session.
This project runs on free-tier providers that have already been observed
hitting multi-minute rate-limit cooldowns under real load (the Groq
2185s-cooldown bug fixed earlier in llm_client.py). Two independent,
hard-enforced limits guard against a runaway sub-agent chain:
  1. MAX_SUBAGENT_DEPTH: a sub-agent's own dispatch_agent tool is NOT
     included in its restricted registry when subagent_depth would exceed
     this -- so a sub-agent literally cannot spawn a further sub-agent past
     the depth limit (matches Claude Code's own documented "subagents
     cannot spawn further subagents" bounded-nesting behavior, but this
     project generalizes it to a configurable depth instead of a hardcoded
     1-level cutoff, since that's a trivial, already-tested extension of
     the same mechanism).
  2. SubagentBudget: a small shared counter object, created fresh by
     run_agent's top-level caller (see agent.py's subagent_budget
     parameter) and threaded down through every dispatch_agent call in the
     SAME top-level task. Exceeding MAX_SUBAGENTS_PER_TASK returns a clear
     ERROR string instead of silently starting another full ReAct loop.
Both defaults are deliberately conservative (depth 1, budget 4 per task)
given this project's free-tier, cooldown-prone provider stack -- these are
plain module-level constants, easy to raise later once/if a paid provider
tier is added to the Router.
"""

from __future__ import annotations

import threading
from typing import Optional

MAX_SUBAGENT_DEPTH = 1        # a sub-agent cannot itself spawn a further sub-agent
MAX_SUBAGENTS_PER_TASK = 4    # hard cap on dispatch_agent calls within one top-level run_agent() task
SUBAGENT_MAX_ITERATIONS = 10  # a sub-agent gets a SMALLER step budget than the parent's default (20)


class SubagentBudget:
    """Shared, thread-safe counter of how many sub-agents have been
    dispatched so far within ONE top-level task. A fresh instance must be
    created per top-level run_agent()/run_mission() call (never reused
    across unrelated tasks) -- see agent.py's `subagent_budget` parameter,
    which defaults to None (meaning "no sub-agents dispatched yet, and no
    shared counter exists" -- dispatch_agent creates one on first use if
    it's ever actually called from a top-level task that didn't set one up
    itself, so ad-hoc/test callers of dispatch_agent don't need to know
    about this class to use it directly)."""

    def __init__(self, max_subagents: int = MAX_SUBAGENTS_PER_TASK) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self.max_subagents = max_subagents

    def try_acquire(self) -> bool:
        """Returns True (and increments the count) if under budget, False
        (no side effect) if the budget is already exhausted."""
        with self._lock:
            if self._count >= self.max_subagents:
                return False
            self._count += 1
            return True

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_subagents - self._count)


# ---------------------------------------------------------------------------
# Sub-agent types: each maps to a restricted TOOL_FUNCTIONS/TOOL_SPECS
# subset and its own narrow system prompt. Tool names here are checked
# directly against tools.TOOL_FUNCTIONS at dispatch time (see
# _restricted_registry) -- a name listed here that isn't actually
# registered (e.g. RAG_AVAILABLE is False in this environment) is simply
# skipped, never crashes.
# ---------------------------------------------------------------------------

# READ_ONLY_TOOL_NAMES is intentionally the ONE canonical catalog of
# "tools that only read/observe, never mutate anything" for this whole
# project -- promoted from a private constant to a public one specifically
# so permissions.py (permission modes) reuses THIS list instead of hand-
# typing a second, parallel one that could silently drift out of sync.
# cache.CACHEABLE_TOOLS is deliberately NOT the same list: it's a much
# narrower "safe to serve a cached result for" set (read_file/list_files/
# grep_files only) tuned for cache-hit correctness, not "every read-only
# tool" -- e.g. git_status/web_search/lsp_find_references are genuinely
# read-only but aren't in CACHEABLE_TOOLS (no caching benefit was designed
# for them yet). permissions.py unions the two rather than picking one.
READ_ONLY_TOOL_NAMES = (
    "read_file", "list_files", "grep_files",
    "lsp_find_references", "lsp_get_diagnostics",
    "rag_search",
    "get_accessibility_snapshot",
    "git_status", "git_diff", "git_log",
    "web_search",
    "filesystem_read_file", "filesystem_read_text_file",
    "filesystem_list_directory", "filesystem_directory_tree",
    "filesystem_search_files", "filesystem_get_file_info",
    "fetch_fetch",
)
_READ_ONLY_TOOL_NAMES = READ_ONLY_TOOL_NAMES  # kept as an alias: this module's own code below used the private name first

# general-purpose: every tool the PARENT agent itself has, minus
# dispatch_agent (nesting is governed by MAX_SUBAGENT_DEPTH, not by
# omitting the tool -- see _restricted_registry). Modeled on Claude Code's
# documented general-purpose agent type ("full tool access, inherits
# context -- for research/multi-step tasks/uncertain searches").
SUBAGENT_TYPES = {
    "general-purpose": {
        "description": "Full tool access (same as the main agent). For multi-step tasks whose exact steps aren't known in advance.",
        "tool_names": None,  # None => every tool the parent has (minus dispatch_agent nesting, handled separately)
    },
    "explore": {
        "description": "Fast, READ-ONLY research: find files, search code, read content, check git/LSP state. Cannot write, edit, or run commands.",
        "tool_names": READ_ONLY_TOOL_NAMES,
    },
    "plan": {
        "description": "Read-only architecture/planning: investigate the codebase and propose an implementation plan in its final answer. Cannot make any changes.",
        "tool_names": READ_ONLY_TOOL_NAMES,
    },
}

_SUBAGENT_SYSTEM_PROMPT_TEMPLATE = """You are a focused sub-agent dispatched by a parent coding agent to

complete ONE specific, self-contained task. You have your own separate
tool set and conversation -- you cannot see the parent's other work, and
the parent cannot see your intermediate steps, only your FINAL answer.

Your available tools: {tool_list}

Task type: {subagent_type} -- {type_description}

Guidelines:
- Complete the task as thoroughly as your available tools allow.
- If a needed capability is outside your tool set (e.g. you were asked to
  make a change but you're a read-only sub-agent), do NOT fabricate having
  done it -- clearly state in your final answer what you found/propose and
  that it needs a follow-up step with write access.
- Never fabricate file contents, search results, or command output.
- Your final answer (the message with no more tool calls) is the ONLY
  thing the parent agent will see -- make it a clear, self-contained
  summary of what you found or did, not a reference to steps the parent
  can't see (e.g. don't say "as shown above").
- Be concise. You are one step in a larger task, not the whole task.
"""


def _restricted_registry(subagent_type: str, subagent_depth: int) -> tuple[dict, list, str]:
    """Build (tool_functions, tool_specs, system_prompt) for `subagent_type`,
    filtered against the REAL currently-registered tools.TOOL_FUNCTIONS /
    tools.TOOL_SPECS (so an unavailable tool -- e.g. RAG_AVAILABLE False in
    an environment without chromadb -- is silently skipped rather than
    causing a KeyError).

    dispatch_agent itself is included in the restricted set ONLY if
    subagent_depth + 1 < MAX_SUBAGENT_DEPTH -- i.e. a sub-agent can spawn
    a further sub-agent only while still under the depth ceiling. At
    depth >= MAX_SUBAGENT_DEPTH - 1 (the default MAX_SUBAGENT_DEPTH=1 means
    this is immediately true for any depth-0 dispatch), the sub-agent's
    own registry has no dispatch_agent entry at all -- structurally cannot
    nest, not just told not to.
    """
    import tools as _tools  # local import: subagents.py is imported BY tools.py at module scope

    all_names = set(_tools.TOOL_FUNCTIONS.keys())
    spec_def = SUBAGENT_TYPES[subagent_type]
    wanted = spec_def["tool_names"]

    if wanted is None:
        allowed_names = set(all_names) - {"dispatch_agent"}
    else:
        allowed_names = set(wanted) & all_names

    if subagent_depth + 1 < MAX_SUBAGENT_DEPTH:
        allowed_names.add("dispatch_agent")

    tool_functions = {name: _tools.TOOL_FUNCTIONS[name] for name in allowed_names}
    tool_specs = [
        spec for spec in _tools.TOOL_SPECS
        if spec.get("function", {}).get("name") in allowed_names
    ]

    tool_list = ", ".join(sorted(allowed_names)) or "(no tools -- text-only reasoning)"
    system_prompt = _SUBAGENT_SYSTEM_PROMPT_TEMPLATE.format(
        tool_list=tool_list,
        subagent_type=subagent_type,
        type_description=spec_def["description"],
    )
    return tool_functions, tool_specs, system_prompt


def _restricted_registry_for_named_agent(
    agent_name: str, subagent_depth: int
) -> tuple[dict, list, str, Optional[int]]:
    """Build (tool_functions, tool_specs, system_prompt, max_iterations)
    for a NAMED custom agent (`.agent_agents/<x>.md`), resolving its full
    `extends` chain via custom_agents.py first.

    Composition with the depth/nesting rule below is IDENTICAL to
    _restricted_registry above (dispatch_agent only re-added while still
    under MAX_SUBAGENT_DEPTH) -- a named agent gets no special exemption
    from the same rate-limit/runaway-nesting protection every other
    sub-agent dispatch already has.

    `tools:` + `mode:` COMPOSE VIA INTERSECTION, NEVER UNION (see
    custom_agents.py's module docstring, decision 3): if the resolved
    agent specifies BOTH a mode (whose own PermissionEngine.
    restricted_registry may already return a narrowed registry) AND an
    explicit `tools:` list, the final allowed set is the INTERSECTION of
    the two -- a named agent's `tools:` can only narrow what its mode
    already restricted, never widen it back. If only one of the two is
    given, that one alone determines the allowed set (matching
    "narrow, don't widen" trivially since there's nothing to intersect
    against).

    Raises ValueError (propagated to the caller, which turns it into a
    clear ERROR string -- never a raw traceback surfaced to the model)
    for an unknown agent name, a broken `extends` link, or an inheritance
    cycle -- see custom_agents.resolve_agent's own docstring.
    """
    resolved = _resolve_named_agent(agent_name)
    return _registry_for_resolved_agent(resolved, agent_name, subagent_depth)


def _resolve_named_agent(agent_name: str):
    """Just the CHEAP, side-effect-free half of
    _restricted_registry_for_named_agent -- scan + resolve the inheritance
    chain, nothing else. Split out so dispatch_agent can validate an
    `agent_name` BEFORE touching the sub-agent depth check or budget,
    exactly mirroring how an invalid `subagent_type` is already validated
    before either of those (see test_unknown_subagent_type_rejected_
    before_any_llm_call in test/subagents_test.py -- a real regression
    this split was added specifically to fix: an earlier version of this
    function validated agent_name only AFTER budget.try_acquire() had
    already run, silently wasting a budget slot on a call that was never
    going to actually dispatch anything)."""
    import custom_agents as _custom_agents  # local import: avoids importing tools.py's own end-of-file dependency chain at module load time

    raw_defs = _custom_agents.scan_agent_defs()
    return _custom_agents.resolve_agent(agent_name, raw_defs)


def _registry_for_resolved_agent(resolved, agent_name: str, subagent_depth: int) -> tuple[dict, list, str, Optional[int]]:
    """The rest of _restricted_registry_for_named_agent's work, given an
    ALREADY-RESOLVED agent config -- builds the actual
    (tool_functions, tool_specs, system_prompt, max_iterations) tuple."""
    import tools as _tools  # local import: same reasoning as _restricted_registry above
    import custom_agents as _custom_agents  # local import: needed for build_agent_system_prompt below

    all_names = set(_tools.TOOL_FUNCTIONS.keys())

    # Start from the mode's own registry (if any), else the full registry.
    if resolved.mode is not None:
        import permissions as _permissions  # local import: see this module's docstring for why this MUST be lazy (subagents<->permissions circular import, confirmed live)
        try:
            mode_enum = _permissions.PermissionMode(resolved.mode)
        except ValueError:
            raise ValueError(
                f"agent '{agent_name}' has mode '{resolved.mode}', which is not a valid "
                f"permission mode. Valid values: {', '.join(m.value for m in _permissions.PermissionMode)}"
            )
        engine = _permissions.PermissionEngine(mode_enum)
        mode_tool_functions, _mode_tool_specs = engine.restricted_registry()
        mode_allowed = set(mode_tool_functions.keys()) if mode_tool_functions is not None else set(all_names)
    else:
        mode_allowed = set(all_names)

    if resolved.tools is not None:
        requested_allowed = set(resolved.tools) & all_names
        allowed_names = mode_allowed & requested_allowed
    else:
        allowed_names = mode_allowed

    allowed_names.discard("dispatch_agent")
    if subagent_depth + 1 < MAX_SUBAGENT_DEPTH:
        allowed_names.add("dispatch_agent")

    tool_functions = {name: _tools.TOOL_FUNCTIONS[name] for name in allowed_names}
    tool_specs = [
        spec for spec in _tools.TOOL_SPECS
        if spec.get("function", {}).get("name") in allowed_names
    ]

    tool_list = ", ".join(sorted(allowed_names)) or "(no tools -- text-only reasoning)"
    system_prompt = _custom_agents.build_agent_system_prompt(resolved, tool_list)

    return tool_functions, tool_specs, system_prompt, resolved.max_iterations


def dispatch_agent(
    prompt: str,

    subagent_type: str = "general-purpose",
    description: str = "",
    agent_name: Optional[str] = None,
    _confirm=None,
    _subagent_depth: int = 0,
    _subagent_budget: Optional[SubagentBudget] = None,
) -> str:
    """
    Delegate a self-contained task to a sub-agent and return its final
    answer. See this module's docstring for the full design rationale.

    `prompt`: the task for the sub-agent to complete (should be fully
    self-contained -- the sub-agent does NOT see the parent's conversation).
    `subagent_type`: one of "general-purpose", "explore", "plan" -- see
    SUBAGENT_TYPES. Determines which tools the sub-agent structurally has
    access to. IGNORED if `agent_name` is given (see below) -- a named
    custom agent's own resolved config takes over entirely.
    `description`: short (3-5 word) label, purely for logging/display --
    has no effect on behavior.
    `agent_name`: optional name of a custom agent definition from
    `.agent_agents/*.md` (see custom_agents.py). When given, `subagent_type`
    is ignored and this dispatch instead uses the named agent's fully
    inheritance-resolved system prompt, tool restriction (composed with
    its `mode:`, if any, via INTERSECTION -- see custom_agents.py's module
    docstring, decision 3), and `max_iterations` override, if specified.
    An unknown `agent_name`, a broken `extends` link, or an inheritance
    cycle returns a clear ERROR string (never a raw traceback) -- the
    SAME graceful-failure posture as an unknown `subagent_type` below.

    `_confirm`/`_subagent_depth`/`_subagent_budget` are NOT part of this
    tool's LLM-visible schema (see tools.py's TOOL_SPECS entry for
    dispatch_agent, which only exposes prompt/subagent_type/description/
    agent_name) -- they're injected by agent._dispatch_tool_call, the same
    pattern already used for run_command's on_line callback. A caller
    invoking this function directly (e.g. a test) that omits them gets
    safe defaults: no confirmation gating (_confirm=None -> agent's own
    interactive default), depth 0, and a fresh budget created on first use.
    """
    # VALIDATION FIRST, before the depth check or budget.try_acquire()
    # below -- matches the pre-existing contract (see
    # test_unknown_subagent_type_rejected_before_any_llm_call in
    # test/subagents_test.py) that an invalid subagent_type must be
    # rejected without spending any budget. A real regression was caught
    # here while adding agent_name: an earlier draft resolved agent_name
    # (which can raise ValueError for an unknown name/broken extends/
    # cycle) AFTER budget.try_acquire() had already run, silently wasting
    # a budget slot on a call that could never actually dispatch anything
    # -- fixed by resolving/validating BOTH subagent_type and agent_name
    # up front, before either the depth check or the budget is touched.
    resolved_named_agent = None
    if agent_name:
        try:
            resolved_named_agent = _resolve_named_agent(agent_name)
        except ValueError as e:
            return f"ERROR: could not dispatch custom agent '{agent_name}': {e}"
    elif subagent_type not in SUBAGENT_TYPES:
        return (
            f"ERROR: unknown subagent_type '{subagent_type}'. "
            f"Valid types: {', '.join(sorted(SUBAGENT_TYPES.keys()))}"
        )

    if _subagent_depth >= MAX_SUBAGENT_DEPTH:
        return (
            f"ERROR: refusing to dispatch a sub-agent at depth {_subagent_depth} "
            f"(MAX_SUBAGENT_DEPTH={MAX_SUBAGENT_DEPTH}) -- sub-agents cannot spawn "
            "further sub-agents past this limit. Complete this step directly "
            "with your own tools instead."
        )

    budget = _subagent_budget if _subagent_budget is not None else SubagentBudget()
    if not budget.try_acquire():
        return (
            f"ERROR: sub-agent budget exhausted (max {budget.max_subagents} per task). "
            "Complete the remaining work directly with your own tools instead of "
            "dispatching another sub-agent."
        )

    import agent as _agent  # local import: avoids a circular import (agent.py imports tools.py, which will import this module)

    max_iterations = SUBAGENT_MAX_ITERATIONS
    if resolved_named_agent is not None:
        try:
            tool_functions, tool_specs, system_prompt, override_max_iterations = (
                _registry_for_resolved_agent(resolved_named_agent, agent_name, _subagent_depth)
            )
        except ValueError as e:
            # Only reachable for an invalid `mode:` value inside the
            # resolved agent (see _registry_for_resolved_agent) --
            # agent-name/extends/cycle errors were already caught above,
            # before the budget was ever touched.
            return f"ERROR: could not dispatch custom agent '{agent_name}': {e}"
        if override_max_iterations is not None:
            max_iterations = override_max_iterations
    else:
        tool_functions, tool_specs, system_prompt = _restricted_registry(subagent_type, _subagent_depth)

    try:
        reply = _agent.run_agent(
            prompt,
            verbose=False,
            log=lambda event, payload: None,
            confirm=_confirm,
            max_iterations=max_iterations,
            system_prompt=system_prompt,
            tool_functions=tool_functions,
            tool_specs=tool_specs,
            persist_memory=False,
            subagent_depth=_subagent_depth + 1,
            subagent_budget=budget,
        )
    except Exception as e:
        return f"ERROR: sub-agent ({subagent_type}) raised an exception: {type(e).__name__}: {e}"

    type_label = f"agent:{agent_name}" if agent_name else subagent_type
    label = description or type_label
    return f"[sub-agent '{label}' ({type_label}) result]\n{reply}"


TOOL_FUNCTIONS = {"dispatch_agent": dispatch_agent}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "dispatch_agent",
            "description": (
                "Delegate a self-contained sub-task to a separate sub-agent with its "
                "own isolated tool loop and conversation, returning only its FINAL "
                "answer (not its intermediate steps) back to you. Use this for "
                "genuinely independent research/exploration work you want done "
                "without cluttering your own context with every intermediate read/"
                "search -- NOT for simple one-tool-call lookups you could just do "
                "yourself directly. Sub-agents are expensive (a full separate multi-"
                "turn loop) and rate-limited (max "
                f"{MAX_SUBAGENTS_PER_TASK} per task, max nesting depth "
                f"{MAX_SUBAGENT_DEPTH}) -- don't dispatch one for trivial work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The FULLY SELF-CONTAINED task for the sub-agent -- it cannot see "
                            "your conversation, so include every relevant detail/file path/"
                            "context it needs."
                        ),
                    },
                    "subagent_type": {
                        "type": "string",
                        "enum": list(SUBAGENT_TYPES.keys()),
                        "description": (
                            "'general-purpose' (full tool access, for multi-step tasks), "
                            "'explore' (fast READ-ONLY research: find/search/read, cannot "
                            "write/edit/run commands), or 'plan' (read-only, proposes an "
                            "implementation plan without making changes). IGNORED if "
                            "agent_name is given instead."
                        ),
                    },
                    "agent_name": {
                        "type": "string",
                        "description": (
                            "Name of a custom agent definition from .agent_agents/*.md (see "
                            "list_custom_agents) to use instead of subagent_type -- gives this "
                            "dispatch that agent's own pre-configured system prompt, tool "
                            "restriction, and permission mode. Leave unset to use subagent_type "
                            "instead."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Short (3-5 word) label for this sub-agent call, for logging only.",
                    },
                },
                "required": ["prompt"],
            },
        },
    }
]
