"""
main.py
-------
The interactive loop: you type a request, the agent thinks (agent.py) and
acts (tools.py), and the result is printed back to you. Conversation state
is persisted to memory.json between runs.

Usage:
    export GROQ_API_KEY=...        # or GOOGLE_API_KEY / CEREBRAS_API_KEY / OPENROUTER_API_KEY
    python main.py                                       # normal mode: prompts before destructive actions
    python main.py --dry-run                             # safe mode: auto-declines destructive/sensitive actions
    python main.py --permission-mode plan                # read-only: propose changes, never execute them
    python main.py --permission-mode accept_edits         # ordinary file writes/edits proceed without asking
    python main.py --permission-mode auto                 # same as accept_edits (see permissions.py for why
                                                            # this project's "auto" does NOT run an LLM safety
                                                            # classifier the way Anthropic's own does)
    python main.py --permission-mode dont_ask              # locked-down: only read-only tools are even callable
    python main.py --permission-mode bypass                # skips confirmation prompts (NOT the unconditional
                                                            # secret-path block inside the tools themselves) --
                                                            # isolated sandboxes/containers only
    python main.py --permission-mode plan --dry-run        # combining modes with --dry-run is supported; see
                                                            # the printed warnings below for interactions worth
                                                            # knowing about (accept_edits/auto/bypass all reduce
                                                            # or defeat what --dry-run protects against)

Session resume (see missions.py for the underlying durable checkpoint
state -- a compact summary/next-step/key-files handoff, NOT a raw replayed
transcript):
    python main.py --resume my-feature                    # scope this whole REPL session to mission
                                                            # 'my-feature' -- loads its saved checkpoint (if
                                                            # any) as context for every turn, and saves a
                                                            # fresh checkpoint after each one. Starts a NEW
                                                            # mission under this id if no checkpoint exists yet
                                                            # (matching missions.load_progress's own "None
                                                            # means first-ever run" semantics) -- this is NOT
                                                            # an error, printed clearly either way.
    python main.py --continue                              # resume the MOST RECENTLY updated mission (the
    python main.py -c                                       # short form) without needing to know/type its id
                                                            # -- errors clearly if no mission has ever been
                                                            # saved (nothing to continue).
    python main.py --list-missions                          # print every saved mission (id, last updated,
                                                            # summary) and exit -- no LLM call, no REPL.
    python main.py --resume my-feature --permission-mode plan   # modes compose with resume/continue --
    python main.py --continue --permission-mode accept_edits    # see permissions.run_mission_with_mode

Non-interactive one-shot mode (for scripts/CI -- runs ONE prompt, prints
the result, and exits; never starts the REPL):
    python main.py --print "list all TODO comments in this project"
    python main.py -p "..." --output-format json           # machine-readable {"result": ..., "mission_id": ...}
    python main.py --print "..." --resume my-feature        # combine with session resume
    python main.py --print "..." --permission-mode plan     # combine with a permission mode

If --permission-mode is omitted entirely, behavior is IDENTICAL to before
permission modes existed (see permissions.py's own module docstring for
why 'default' mode's own system-prompt suffix is skipped here specifically
to guarantee that byte-for-byte equivalence, not just close-enough). The
same "omitted -> exact prior behavior" guarantee holds for every flag
documented above -- none of them change anything about a plain
`python main.py` invocation with no flags at all.
"""

from __future__ import annotations

import atexit
import json as _json
import sys

from agent import run_agent, run_mission
import memory
import missions
import permissions
import process_manager


BANNER = """\
my-agent — a tiny local coding agent
Type your request, or one of: /reset, /memory, /exit, /<skill-or-command-name> [args]
"""


def _try_expand_slash_invocation(user_input: str) -> str | None:
    """Real, direct `/name [args]` invocation of a skill or plugin command
    -- the one genuinely missing capability once you account for Claude
    Code's own current docs saying "custom commands have been merged into
    skills" (see plugins.py's module docstring, decision 1): today, only
    the LLM itself can decide to call the `load_skill` tool -- a human
    typing `/name` at this prompt had no way to force that before this.

    Returns the EXPANDED PROMPT TEXT to send to the agent (the skill's
    real body + any trailing args appended, so it becomes an ordinary
    user_input string flowing through the exact same run_agent()/
    run_agent_with_mode() call below -- no new agent-side code path), or
    None if `user_input` isn't a `/name` invocation of anything real
    (including the built-in /reset, /memory, /exit, which are checked
    BEFORE this function is ever called, so they're never shadowed).
    """
    if not user_input.startswith("/") or user_input in ("/exit", "/quit", "/reset", "/memory"):
        return None

    try:
        import plugins
    except Exception:
        return None
    if not getattr(plugins, "PLUGINS_AVAILABLE", False):
        return None

    rest = user_input[1:]
    name, _, args = rest.partition(" ")
    if not name:
        return None

    skill = plugins.find_invocable_skill(name)
    if skill is None:
        return None

    prompt = skill.body
    if args.strip():
        prompt = f"{prompt}\n\nAdditional arguments from the user: {args.strip()}"
    return prompt

_VALID_MODE_NAMES = {m.value for m in permissions.PermissionMode}


def _dry_run_confirm(name: str, args: dict, reason: str, diff: str | None = None) -> bool:
    """--dry-run confirmation policy: never actually run flagged actions,
    just report what would have happened (including the diff, if any)."""
    print(f"\n🛑 DRY RUN — would have asked to confirm and then run:", file=sys.stderr)
    print(f"    Tool: {name}({ {k: v for k, v in args.items() if k != 'content'} })", file=sys.stderr)
    print(f"    Reason flagged: {reason}", file=sys.stderr)
    if diff:
        print(f"\n{diff}\n", file=sys.stderr)
    print(f"    Skipping (dry-run mode never executes flagged actions).", file=sys.stderr)
    return False


def _parse_permission_mode(argv: list[str]) -> permissions.PermissionMode | None:
    """Returns the requested PermissionMode, or None if --permission-mode
    wasn't passed at all -- None is the signal main() uses to take the
    EXACT prior code path (plain run_agent(), no permissions.py involved),
    rather than routing through run_agent_with_mode() with mode=DEFAULT
    (which would append DEFAULT's own system-prompt suffix and technically
    change the prompt sent to the model, even though DEFAULT's actual
    confirm() behavior is identical -- avoiding that keeps main.py's
    no-flags behavior provably unchanged, not just functionally similar)."""
    if "--permission-mode" not in argv:
        return None
    idx = argv.index("--permission-mode")
    if idx + 1 >= len(argv):
        print(
            "[error] --permission-mode requires a value, e.g. --permission-mode plan\n"
            f"        Valid values: {', '.join(sorted(_VALID_MODE_NAMES))}",
            file=sys.stderr,
        )
        sys.exit(1)
        return None  # unreachable in real use (sys.exit raises SystemExit) --
        # kept explicit so a mocked sys.exit() (e.g. in tests) can't fall
        # through to the IndexError below instead of stopping cleanly;
        # caught directly by test_missing_value_after_flag_exits_with_error.
    value = argv[idx + 1]
    if value not in _VALID_MODE_NAMES:
        print(
            f"[error] unknown --permission-mode value '{value}'.\n"
            f"        Valid values: {', '.join(sorted(_VALID_MODE_NAMES))}",
            file=sys.stderr,
        )
        sys.exit(1)
        return None  # same reasoning as above
    return permissions.PermissionMode(value)


def _warn_about_dry_run_mode_interaction(mode: permissions.PermissionMode) -> None:
    """--dry-run's guarantee ('nothing flagged actually executes') only
    holds as strongly as the active mode's confirm_fn actually consults
    base_confirm (=_dry_run_confirm here) before deciding. Two real modes
    partially or fully bypass that consultation by design (see
    permissions.py's PermissionEngine.confirm_fn) -- surfaced here as an
    explicit, printed warning rather than a silent surprise, since a user
    combining --dry-run with a mode is very likely assuming --dry-run's
    protection is unconditional."""
    if mode == permissions.PermissionMode.BYPASS:
        print(
            "⚠️  WARNING: --permission-mode bypass NEVER consults --dry-run's decline logic at all "
            "(bypass mode auto-approves every prompted action unconditionally). Combining these flags "
            "means --dry-run has NO effect this session -- everything will actually execute. "
            "(The one thing bypass still cannot do is touch a secret/credential path -- that block is "
            "unconditional inside the tools themselves, independent of any mode or --dry-run.)",
            file=sys.stderr,
        )
    elif mode in (permissions.PermissionMode.ACCEPT_EDITS, permissions.PermissionMode.AUTO):
        print(
            f"⚠️  NOTE: --permission-mode {mode.value} auto-approves ORDINARY file writes/edits without "
            "consulting --dry-run at all -- those will actually happen for real this session. Only "
            "genuinely destructive shell commands and sensitive-path attempts still go through "
            "--dry-run's decline-and-report behavior.",
            file=sys.stderr,
        )
    # default/plan/dont_ask all either reproduce default's existing
    # base_confirm consultation exactly, or structurally exclude every
    # tool --dry-run would otherwise need to protect against (plan/
    # dont_ask's registries never even contain write_file/run_command) --
    # no warning needed for those combinations.


def _extract_flag_value(argv: list[str], *flag_names: str) -> str | None:
    """Shared helper: find the FIRST occurrence of any of `flag_names` in
    argv and return its following value, or None if none of them are
    present. Exits with a clear error (matching _parse_permission_mode's
    own established error style) if the flag is present but has no value
    after it. Used for --resume/--print/--output-format, which all share
    this exact "flag NAME_OR_TEXT" shape."""
    for flag in flag_names:
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 >= len(argv):
                print(f"[error] {flag} requires a value.", file=sys.stderr)
                sys.exit(1)
                return None  # unreachable in real use -- see _parse_permission_mode's own comment on why this is kept explicit
            return argv[idx + 1]
    return None


def _parse_mission_selection(argv: list[str]) -> tuple[str | None, bool]:
    """Returns (mission_id_or_None, is_continue). At most one of
    --resume/-r and --continue/-c may be given -- combining them is
    rejected with a clear error rather than silently picking one,
    matching this project's standing "never silently guess" posture.

    `-r`/`-c` short forms are real, documented Claude Code shorthand
    (verified this session against multiple current explainers) --
    included here for the same muscle-memory reason --permission-mode's
    long form alone was judged sufficient for that flag (a NEW-to-this-
    project mode name benefits less from a terse alias than a widely-
    known resume/continue pair does).
    """
    has_resume = "--resume" in argv or "-r" in argv
    has_continue = "--continue" in argv or "-c" in argv
    if has_resume and has_continue:
        print(
            "[error] --resume/-r and --continue/-c are mutually exclusive -- "
            "pick one (a specific mission id, or the most recently updated one).",
            file=sys.stderr,
        )
        sys.exit(1)
        return None, False

    if has_resume:
        # _extract_flag_value ALREADY exits(1) with a clear message if
        # --resume/-r is present with no value after it -- no separate
        # check needed here (an earlier draft had a second, redundant
        # "if mission_id is None: sys.exit(1)" that was genuinely
        # unreachable in real execution, since sys.exit actually stops
        # the process; caught by a test that mocks sys.exit specifically
        # to make this class of bug visible, the same technique
        # test_missing_value_after_flag_exits_with_error already
        # established for _parse_permission_mode).
        mission_id = _extract_flag_value(argv, "--resume", "-r")
        return mission_id, False

    if has_continue:
        return None, True

    return None, False


def _resolve_continue_mission_id() -> str:
    """--continue/-c resumes the MOST RECENTLY UPDATED mission (real,
    documented Claude Code semantics: "-c ... Loads the most recent
    session ... Zero configuration required") -- reuses
    missions.list_missions(), which already returns missions sorted most-
    recently-updated-first (see that function's own docstring), rather
    than re-deriving the same sort here a second time. Exits with a
    clear error if no mission has EVER been saved -- there is nothing to
    continue, and silently falling back to a brand-new unnamed session
    would contradict what --continue is asking for."""
    all_missions = missions.list_missions()
    if not all_missions:
        print(
            "[error] --continue/-c found no saved missions to resume. "
            "Start a new one with --resume <name>, or run without a session flag "
            "for a plain (non-mission) session.",
            file=sys.stderr,
        )
        sys.exit(1)
        return ""  # unreachable in real use
    return all_missions[0]["mission_id"]


def _print_mission_list() -> None:
    """--list-missions: print every saved mission and exit -- no LLM
    call, no REPL, matching real Claude Code's `--list` (prints and
    exits, doesn't enter a session)."""
    all_missions = missions.list_missions()
    if not all_missions:
        print("(no saved missions yet -- start one with --resume <name>)")
        return
    for m in all_missions:
        print(f"- {m['mission_id']}  (updated {m['updated_at']})")
        print(f"    {m['summary'][:200]}{'...' if len(m['summary']) > 200 else ''}")


def _run_one_shot(
    prompt: str,
    mission_id: str | None,
    mode: permissions.PermissionMode | None,
    confirm,
    output_format: str,
) -> None:
    """--print/-p: run ONE prompt to completion, print the result, and
    return -- never starts the REPL loop. Reuses the exact same
    run_agent/run_mission/run_agent_with_mode/run_mission_with_mode call
    paths the interactive loop uses below (see main()'s own REPL body) --
    NOT a separate, parallel execution path that could silently drift
    out of sync with the interactive one's actual behavior.

    `output_format`: "text" (default -- just the reply, like the real
    Claude Code default) or "json" (a single machine-readable JSON object
    on stdout: {"result": ..., "mission_id": ...} -- deliberately a small
    real subset of Claude Code's own documented JSON result object
    (which also includes cost/duration/turn-count fields this project
    doesn't currently track anywhere; not fabricated here just to look
    more complete).
    """
    try:
        if mission_id is not None:
            if mode is None:
                reply = run_mission(prompt, mission_id=mission_id, verbose=False, confirm=confirm)
            else:
                reply = permissions.run_mission_with_mode(
                    prompt, mission_id=mission_id, mode=mode, base_confirm=confirm, verbose=False,
                )
        else:
            if mode is None:
                reply = run_agent(prompt, verbose=False, confirm=confirm)
            else:
                reply = permissions.run_agent_with_mode(prompt, mode=mode, base_confirm=confirm, verbose=False)
    except Exception as e:
        if output_format == "json":
            print(_json.dumps({"error": str(e), "mission_id": mission_id}))
        else:
            print(f"[error] agent failed: {e}", file=sys.stderr)
        sys.exit(1)
        return

    if output_format == "json":
        print(_json.dumps({"result": reply, "mission_id": mission_id}))
    else:
        print(reply)


def main() -> None:
    argv = sys.argv[1:]

    # --list-missions is handled FIRST and unconditionally exits -- no
    # LLM call, no REPL, no startup-cleanup side effects, matching real
    # Claude Code's own --list (a pure read of already-saved state).
    if "--list-missions" in argv:
        _print_mission_list()
        return

    dry_run = "--dry-run" in argv
    mode = _parse_permission_mode(argv)
    confirm = _dry_run_confirm if dry_run else None  # None -> agent.py's default y/N prompt

    if mode is not None and dry_run:
        _warn_about_dry_run_mode_interaction(mode)

    mission_id, is_continue = _parse_mission_selection(argv)
    if is_continue:
        mission_id = _resolve_continue_mission_id()

    one_shot_prompt = _extract_flag_value(argv, "--print", "-p")
    output_format = _extract_flag_value(argv, "--output-format") or "text"
    if output_format not in ("text", "json"):
        print(f"[error] unknown --output-format value '{output_format}'. Valid values: text, json", file=sys.stderr)
        sys.exit(1)

    if one_shot_prompt is not None:
        # Non-interactive one-shot: no banner, no startup-orphan-cleanup
        # noise mixed into stdout (which --output-format json callers may
        # be parsing), no atexit REPL cleanup registration needed beyond
        # what process_manager already does for background processes
        # started DURING this one call.
        atexit.register(process_manager.cleanup_all)
        _run_one_shot(one_shot_prompt, mission_id, mode, confirm, output_format)
        return

    # Kill anything left running from a PREVIOUS session that crashed/was
    # killed before it could clean up after itself (e.g. a Flask dev server
    # started via start_background_process). Explicitly done here at the
    # REPL's real startup boundary -- NOT automatically on module import,
    # since a background process is often meant to outlive the specific
    # tool call that started it and be used by later calls in the SAME
    # session; only a genuinely NEW session should assume anything still
    # tracked is orphaned. See process_manager.py's module docstring.
    startup_cleanup = process_manager.cleanup_orphans_from_previous_run()
    if startup_cleanup and "nothing" not in startup_cleanup:
        print(f"[startup cleanup] {startup_cleanup}")

    # Best-effort safety net for THIS session's own process lifetime: fires
    # on normal exit, /exit, Ctrl-C, or an uncaught exception. Does not fire
    # on SIGKILL -- that gap is covered by the next session's startup
    # cleanup above.
    atexit.register(process_manager.cleanup_all)

    print(BANNER)
    if dry_run:
        print("🛡️  Running in --dry-run mode: destructive/sensitive actions will be skipped, not executed.\n")
    if mode is not None:
        print(f"🔐 Permission mode: {mode.value}\n")
    if mission_id is not None:
        prior = missions.load_progress(mission_id)
        if prior:
            print(f"📋 Resuming mission '{mission_id}' (checkpoint saved {prior['updated_at']})\n")
        else:
            print(f"📋 Starting new mission '{mission_id}' (no prior checkpoint found)\n")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break

        if not user_input:
            continue

        if user_input in ("/exit", "/quit"):
            print("bye!")
            break

        if user_input == "/reset":
            memory.reset()
            print("(memory cleared)")
            continue

        if user_input == "/memory":
            print(memory.load())
            continue

        if user_input.startswith("/"):
            expanded = _try_expand_slash_invocation(user_input)
            if expanded is not None:
                print(f"[expanding /{user_input[1:].split(' ', 1)[0]} into a task for the agent]")
                user_input = expanded
            elif user_input not in ("/exit", "/quit", "/reset", "/memory"):
                print(
                    f"[no skill or plugin command named '{user_input[1:].split(' ', 1)[0]}' found -- "
                    "sending this as a plain message instead]"
                )

        try:
            # verbose=True prints each Thought/Action/Observation step live
            # as the ReAct loop runs, before the final reply is returned.
            # confirm=None uses agent.py's default interactive y/N prompt for
            # destructive/sensitive actions, unless --dry-run is active.
            #
            # mission_id is None (the default, no --resume/--continue flag
            # given) and mode is None (no --permission-mode flag given)
            # -> the EXACT prior call, untouched, so existing behavior is
            # provably unchanged when neither new flag is used at all.
            if mission_id is not None:
                if mode is None:
                    reply = run_mission(user_input, mission_id=mission_id, verbose=True, confirm=confirm)
                else:
                    reply = permissions.run_mission_with_mode(
                        user_input, mission_id=mission_id, mode=mode, base_confirm=confirm, verbose=True,
                    )
            else:
                if mode is None:
                    reply = run_agent(user_input, verbose=True, confirm=confirm)
                else:
                    reply = permissions.run_agent_with_mode(
                        user_input, mode=mode, base_confirm=confirm, verbose=True,
                    )
        except Exception as e:
            print(f"[error] agent failed: {e}", file=sys.stderr)
            continue

        print(f"\nagent> {reply}\n")


if __name__ == "__main__":
    main()

