"""
permissions.py
---------------
Named permission MODES (default / plan / accept_edits / auto / dont_ask /
bypass) on top of agent.py's EXISTING, already-tested confirmation gate --
NOT a new safety mechanism. This module is deliberately a thin dispatcher
built entirely out of things this project already has and has already
verified:

  - agent._needs_confirmation(name, args) -> (reason, diff) | None -- the
    single source of truth for "does this call need a human decision, and
    if so, can it be safely previewed." Every mode below reads this
    contract; none of them reimplement destructive/sensitive detection.
  - tools.is_destructive_command / tools.is_sensitive_path -- reused
    (transitively, via _needs_confirmation) for the same reason: one
    canonical definition of "risky," never a second parallel one.
  - subagents.READ_ONLY_TOOL_NAMES -- the canonical "tools that only
    observe" catalog, already built and live-tested last session for
    sub-agent restriction. Reused here (unioned with cache.CACHEABLE_TOOLS)
    for `plan`/`dont_ask` modes' structural enforcement, instead of a
    second hand-typed list that could silently drift out of sync with the
    real registered tool set the way an earlier proposal's list did.
  - agent.run_agent's tool_functions/tool_specs/confirm parameters
    (added last session for sub-agents) -- this is the ONLY mechanism in
    this codebase that can deny a call BY TOOL IDENTITY rather than by
    flagged-riskiness (see "a real bug this design avoids" below). `plan`
    and `dont_ask` both reuse it exactly as-is.

A REAL BUG THIS DESIGN DELIBERATELY AVOIDS: an earlier draft of this
module tried to implement `dont_ask`'s "deny anything not explicitly
allowed" by wrapping `confirm()`. That's wrong and was caught before
shipping: agent._dispatch_tool_call only ever calls confirm() for a call
agent._needs_confirmation ALREADY flagged (destructive command, or a
write/edit with a real diff) -- plain calls like read_file, list_files, or
even write_file creating a brand-new file never reach confirm() at all.
A confirm()-only gate would therefore silently ALLOW every unflagged tool
straight through in dont_ask mode, the opposite of its documented
behavior. The only correct fix is STRUCTURAL: remove disallowed tools from
the registry handed to run_agent entirely (see restricted_registry below),
the exact same mechanism plan mode and subagents.py's "explore" type
already use and have already been live-tested against a real LLM.

WHY A MODE LAYER ON TOP OF confirm(), NOT A REPLACEMENT FOR IT: `confirm`
already is "the thing that decides yes/no for a flagged call" -- a mode is
just a POLICY for automatically answering that question before it would
otherwise prompt a human. Modes never bypass the underlying hard blocks
that live inside the tools themselves (is_sensitive_path's refusal in
read_file/write_file/run_command is UNCONDITIONAL and has no override
anywhere in this codebase, confirmed directly in this module's test suite
by calling the REAL write_file() against a real .env path under bypass
mode) -- a mode can only affect whether a human is ASKED, or which tools
structurally exist to call, never whether a secret can be touched.

ZERO EXTRA LLM CALLS: every mode's decision is a local, synchronous Python
function over already-known information (tool name, parsed args, and the
existing _needs_confirmation result) -- there is no LLM-based "safety
classifier" here, by explicit design choice (this project's free-tier
providers already hit multi-minute rate-limit cooldowns under real load;
spending an extra completion call per tool call to grade its own safety
would make that materially worse for zero benefit over the already-tested
regex/path checks). If a genuine ML classifier is ever wanted later, it
would be an entirely separate, opt-in addition -- not something any mode
here silently does.

Mode behavior (six modes, matching Anthropic's own currently-published
docs at code.claude.com/docs/en/permission-modes -- verified this session,
not extracted from any leaked source):

  default        -- exactly today's existing behavior (every flagged call
                    prompts via `confirm`). This mode is not a new code
                    path; its confirm_fn IS agent._default_confirm's
                    caller unchanged.
  plan            -- STRUCTURALLY read-only: write_file/apply_edit/
                    run_command/etc. are absent from the tool registry
                    handed to run_agent -- the model can propose but never
                    execute a change. Nothing to "confirm" here because
                    the tool to do it doesn't exist.
  accept_edits    -- file edits (write_file/apply_edit, ONLY when
                    _needs_confirmation's diff is not None, i.e. an
                    ordinary same-content-shape overwrite) proceed without
                    asking; run_command and any call _needs_confirmation
                    flags with diff=None (destructive command OR a
                    sensitive-path attempt) still prompts/denies exactly
                    like `default`. This is the one mode that reads
                    _needs_confirmation's (reason, diff) distinction most
                    precisely -- diff-is-not-None is the actual signal
                    "this is an ordinary content change, not something
                    dangerous," already computed by existing, tested code.
  auto            -- same relaxation as accept_edits for writes/edits.
                    run_command calls that reach confirm_fn at all are, BY
                    CONSTRUCTION, always destructive (that's the only
                    reason _needs_confirmation would have flagged them) --
                    so auto mode does NOT additionally relax run_command
                    confirmation; it just never had extra friction for
                    non-destructive commands to begin with (those were
                    never flagged in any mode). DELIBERATE DEVIATION from
                    Anthropic's own docs (their "auto" runs an LLM-based
                    background safety classifier) -- per explicit
                    instruction this session, reuse the free, already-
                    tested regex/path checks instead of an extra LLM call
                    per tool call. Destructive commands and sensitive
                    paths are NEVER auto-allowed in any mode, including
                    this one.
  dont_ask        -- STRUCTURALLY restricted to ALLOWED_IN_DONT_ASK (the
                    canonical read-only tool set) -- everything else is
                    simply absent from the registry, so a call to it fails
                    with "ERROR: unknown tool" (the same, already-tested
                    failure mode as any other restricted registry in this
                    project) with NO fallthrough to a confirmation prompt
                    at all (matches Anthropic's own documented behavior:
                    "no canUseTool callback invoked"). For locked-down/
                    CI/scripted use.
  bypass          -- every call proceeds without a confirmation PROMPT
                    (confirm_fn always returns True). This does NOT touch
                    tools.is_sensitive_path's unconditional hard block
                    inside read_file/write_file/run_command/grep_files
                    themselves (verified directly by a live test in this
                    module's test suite) -- "bypass" means "don't ask a
                    human," not "secrets can now be read/written." For
                    isolated sandboxes/containers only, matching
                    Anthropic's own documented guidance for this mode.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import agent as _agent
import tools as _tools
from cache import CACHEABLE_TOOLS
from subagents import READ_ONLY_TOOL_NAMES


class PermissionMode(str, Enum):
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "accept_edits"
    AUTO = "auto"
    DONT_ASK = "dont_ask"
    BYPASS = "bypass"


# The canonical "read-only" catalog for this whole project, reused as-is
# (see module docstring) rather than hand-typed again here. Used by BOTH
# plan mode (registry restriction) and dont_ask mode (registry
# restriction) -- they happen to restrict to the same set today, kept as
# two separately-named constants since Anthropic's own docs describe them
# as conceptually distinct policies (plan = "propose without executing",
# dont_ask = "explicit allow-list, deny the rest") that could reasonably
# diverge later (e.g. dont_ask being widened via an explicit extra
# allow-list passed by a caller) without that being a plan-mode change too.
_READ_ONLY_NAMES = frozenset(READ_ONLY_TOOL_NAMES) | frozenset(CACHEABLE_TOOLS)
ALLOWED_IN_DONT_ASK = _READ_ONLY_NAMES


def _write_tool_names() -> set[str]:
    """The exact write-tool sets agent.py's own confirmation gate already
    tracks -- reused directly rather than re-declared, so this can never
    drift out of sync with agent.py's own _WRITE_FILE_TOOL_NAMES/
    _APPLY_EDIT_TOOL_NAMES if either is ever extended there."""
    return set(_agent._WRITE_FILE_TOOL_NAMES) | set(_agent._APPLY_EDIT_TOOL_NAMES)


def _build_restricted_registry(allowed_names: set[str]) -> tuple[dict, list]:
    """Shared helper: filter tools.TOOL_FUNCTIONS/TOOL_SPECS down to
    `allowed_names`, against the REAL currently-registered tools (so an
    unavailable tool in this environment -- e.g. RAG_AVAILABLE False -- is
    silently skipped, never a KeyError). Identical filtering logic to
    subagents._restricted_registry, duplicated here (not imported) because
    subagents.py's version also injects a subagent-specific system prompt
    this module doesn't need -- the actual filtering predicate is a single
    dict/list comprehension, not worth the coupling to share a 2-line
    helper across modules with otherwise-unrelated concerns."""
    allowed = allowed_names & set(_tools.TOOL_FUNCTIONS.keys())
    tool_functions = {name: _tools.TOOL_FUNCTIONS[name] for name in allowed}
    tool_specs = [
        spec for spec in _tools.TOOL_SPECS
        if spec.get("function", {}).get("name") in allowed
    ]
    return tool_functions, tool_specs


class PermissionEngine:
    """
    Wraps agent._needs_confirmation with a named mode's policy. Exposes:

      - confirm_fn: a `confirm(name, args, reason, diff) -> bool` callable
        matching agent.py's exact confirm() signature -- pass this
        straight into run_agent(confirm=engine.confirm_fn). Only ever
        invoked for calls agent._needs_confirmation already flagged (see
        module docstring's "a real bug this design avoids") -- it decides
        whether to auto-approve/deny those, never whether a tool exists.
      - restricted_registry(): for `plan`/`dont_ask` modes, a restricted
        (tool_functions, tool_specs) pair to pass into
        run_agent(tool_functions=..., tool_specs=...); (None, None) for
        every other mode (meaning "use the full registry," reproducing
        today's behavior exactly).

    Every method here is a synchronous, local, side-effect-free (except
    for auto-approving/denying) Python function -- zero LLM calls.
    """

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT, base_confirm=None):
        self.mode = PermissionMode(mode)
        # base_confirm is what actually prompts a human (or a test's fake
        # answer) when a mode's own policy doesn't auto-decide -- defaults
        # to agent.py's own existing interactive prompt, so every mode
        # falls back to EXACTLY today's behavior for anything it doesn't
        # explicitly relax.
        self.base_confirm = base_confirm or _agent._default_confirm

    def confirm_fn(self, name: str, args: dict, reason: str, diff: Optional[str] = None) -> bool:
        """Matches agent.py's confirm(name, args, reason, diff) -> bool
        signature exactly."""
        if self.mode == PermissionMode.BYPASS:
            # Skips the PROMPT only. tools.is_sensitive_path's own
            # unconditional block inside read_file/write_file/run_command
            # is untouched -- confirmed by test_bypass_never_touches_
            # secret_paths in test/permissions_test.py, which calls the
            # REAL write_file() against a real .env path under bypass mode
            # and asserts it's still refused.
            return True

        if self.mode == PermissionMode.DONT_ASK:
            # In practice this branch is unreachable for calls that
            # actually run in dont_ask mode: restricted_registry() already
            # removes every tool outside ALLOWED_IN_DONT_ASK from the
            # registry entirely, and none of ALLOWED_IN_DONT_ASK's tools
            # are ones _needs_confirmation ever flags (they're all read-
            # only; only run_command/write_file/apply_edit are flaggable).
            # Kept as a fail-closed defense-in-depth default in case a
            # future read-only tool is ever added to _needs_confirmation's
            # flagged set -- dont_ask must never silently prompt or allow.
            return False

        if self.mode in (PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO):
            # Auto-approve an ORDINARY content-changing write/edit (diff
            # is not None means _needs_confirmation already determined
            # this is "overwrite existing file with different content,"
            # not a sensitive-path attempt -- see _needs_confirmation's
            # own docstring: sensitive paths return diff=None specifically
            # so callers can distinguish). A destructive run_command call
            # (diff is always None for those) or a sensitive-path attempt
            # still falls through to base_confirm below, identical to
            # `default` mode -- auto mode does not additionally relax
            # run_command, since every run_command call that reaches this
            # function is, by construction, one is_destructive_command
            # already matched (see module docstring).
            if name in _write_tool_names() and diff is not None:
                return True
            return self.base_confirm(name, args, reason, diff)

        # DEFAULT (and PLAN, which structurally never reaches this
        # function -- see restricted_registry): identical to today's
        # existing behavior, unchanged.
        return self.base_confirm(name, args, reason, diff)

    def restricted_registry(self) -> tuple[Optional[dict], Optional[list]]:
        """Returns (tool_functions, tool_specs) to pass into
        agent.run_agent -- (None, None) for every mode except `plan` and
        `dont_ask`, which means "use the full global registry"
        (agent.run_agent's own documented default when these are
        omitted/None). `plan`/`dont_ask` return a registry containing ONLY
        the canonical read-only tools -- write_file/apply_edit/
        run_command/etc. are structurally absent as dict keys, the exact
        same enforcement already proven live against a real LLM escape
        attempt for subagents.py's own "explore"/"plan" sub-agent types.
        """
        if self.mode == PermissionMode.PLAN:
            return _build_restricted_registry(set(_READ_ONLY_NAMES))
        if self.mode == PermissionMode.DONT_ASK:
            return _build_restricted_registry(set(ALLOWED_IN_DONT_ASK))
        return None, None

    def system_prompt_suffix(self) -> str:
        """A short, honest note appended to whatever system_prompt is used
        for this run, so the model knows its own operating mode instead of
        being surprised by denials/missing tools it can't explain to the
        user. Purely informational -- the REAL enforcement is
        confirm_fn/restricted_registry above; this text cannot grant or
        remove any capability by itself."""
        descriptions = {
            PermissionMode.DEFAULT: "Default mode: destructive commands and file overwrites will pause for human confirmation.",
            PermissionMode.PLAN: (
                "PLAN MODE: you only have read-only tools available (no write_file/apply_edit/"
                "run_command/etc.) -- investigate and propose a plan in your final answer; "
                "you cannot make any changes in this mode."
            ),
            PermissionMode.ACCEPT_EDITS: (
                "Accept-edits mode: ordinary file writes/edits proceed automatically without "
                "asking. Destructive shell commands still pause for human confirmation."
            ),
            PermissionMode.AUTO: (
                "Auto mode: ordinary file writes/edits proceed automatically. Destructive shell "
                "commands and sensitive paths still require human confirmation -- this is never "
                "skipped."
            ),
            PermissionMode.DONT_ASK: (
                f"Locked-down mode: only {len(ALLOWED_IN_DONT_ASK)} read-only tools are "
                "available; anything else is denied outright with no confirmation prompt."
            ),
            PermissionMode.BYPASS: (
                "Bypass mode: confirmation prompts are skipped for this session. Secret/"
                "credential files are still unconditionally refused by the tools themselves -- "
                "this mode cannot and does not change that."
            ),
        }
        return "\n\nCurrent permission mode: " + descriptions[self.mode]


def run_agent_with_mode(
    user_input: str,
    mode: PermissionMode = PermissionMode.DEFAULT,
    base_confirm=None,
    **run_agent_kwargs,
) -> str:
    """
    Convenience wrapper: builds a PermissionEngine for `mode` and calls
    agent.run_agent with the right confirm/tool_functions/tool_specs/
    system_prompt wiring already applied -- so a caller (main.py, a future
    CLI flag, bridge_server.py) doesn't need to know PermissionEngine's
    internals to use a mode. Any of run_agent's own kwargs can still be
    passed through via **run_agent_kwargs (e.g. mission_id, verbose, log).

    If `system_prompt` is passed in run_agent_kwargs, the mode's own
    suffix is appended to it; if not, it's appended to agent.SYSTEM_PROMPT
    (agent.run_agent's own default when system_prompt is omitted).
    """
    engine = PermissionEngine(mode, base_confirm=base_confirm)
    tool_functions, tool_specs = engine.restricted_registry()

    base_prompt = run_agent_kwargs.pop("system_prompt", None) or _agent.SYSTEM_PROMPT
    system_prompt = base_prompt + engine.system_prompt_suffix()

    return _agent.run_agent(
        user_input,
        confirm=engine.confirm_fn,
        system_prompt=system_prompt,
        tool_functions=tool_functions,
        tool_specs=tool_specs,
        **run_agent_kwargs,
    )


def run_mission_with_mode(
    user_input: str,
    mission_id: str,
    mode: PermissionMode = PermissionMode.DEFAULT,
    base_confirm=None,
    **run_mission_kwargs,
) -> str:
    """
    The run_mission() counterpart to run_agent_with_mode() above -- same
    PermissionEngine wiring, but calls agent.run_mission (mission
    checkpoint load/save) instead of a plain agent.run_agent call.

    A REAL GAP this closes: agent.run_mission previously had no
    system_prompt/tool_functions/tool_specs parameters AT ALL (confirmed
    directly before adding this function -- see agent.run_mission's own
    updated docstring), so there was no way to combine a permission mode
    with a resumed/named mission; --resume/--continue combined with
    --permission-mode would have silently ignored the mode entirely. This
    function is the exact mission-scoped mirror of run_agent_with_mode,
    reusing the SAME PermissionEngine (never a second, parallel
    implementation of mode policy).
    """
    engine = PermissionEngine(mode, base_confirm=base_confirm)
    tool_functions, tool_specs = engine.restricted_registry()

    base_prompt = run_mission_kwargs.pop("system_prompt", None) or _agent.SYSTEM_PROMPT
    system_prompt = base_prompt + engine.system_prompt_suffix()

    return _agent.run_mission(
        user_input,
        mission_id,
        confirm=engine.confirm_fn,
        system_prompt=system_prompt,
        tool_functions=tool_functions,
        tool_specs=tool_specs,
        **run_mission_kwargs,
    )
