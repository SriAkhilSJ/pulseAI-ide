"""
agent.py
--------
The "brain" of the agent: the system prompt, the LLM call, and the
think -> act -> observe loop that keeps calling tools until the model
is ready to give a final answer to the user.

Talks to the LLM via llm_client.chat_completion(), which is backed by a
LiteLLM Router spanning multiple free-tier providers (Groq, Gemini,
Cerebras, OpenRouter). If one provider is rate-limited or down, the Router
automatically fails over to the next configured provider — the agent loop
itself doesn't need to know or care which provider actually answered.

Set whichever of these are available as environment variables (any subset,
at least one required): GROQ_API_KEY, GOOGLE_API_KEY, CEREBRAS_API_KEY,
OPENROUTER_API_KEY.
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import litellm

import tools
import process_manager
from tools import TOOL_FUNCTIONS, TOOL_SPECS
from cache import ToolCache, CACHEABLE_TOOLS
import llm_client
import memory
import missions

MAX_TOOL_ITERATIONS = 20  # safety valve so the agent can't loop forever
# Raised from an original 8 after a real, reproduced stress-test failure:
# a genuinely multi-step task (Flask + SQLite + 3 curl verifications +
# frontend JS update + screenshot) legitimately needs more than 8 tool
# calls to complete honestly -- hitting the old cap didn't cause data loss
# (the checkpoint/backup systems held up fine), but it did mean the task
# was reported as incomplete when it was doing real, correct work. 20 is
# still a real ceiling (an infinite/runaway loop still gets stopped), just
# sized to fit realistic multi-tool tasks instead of a small demo.
MAX_PARALLEL_TOOLS = 6   # cap on concurrent tool calls within one LLM turn

SYSTEM_PROMPT = """You are a helpful coding assistant agent running in a local
sandbox. You have these tools: read_file, write_file, apply_edit, run_command,
list_files, grep_files, undo_last_edit, start_background_process,
stop_background_process, list_background_processes""" + (
    ", screenshot_url, test_local_html, evaluate_js, get_accessibility_snapshot"
    if tools.BROWSER_TOOLS_AVAILABLE else ""
) + (
    ", web_search"
    if getattr(tools, "WEB_SEARCH_AVAILABLE", False) else ""
) + (
    ", generate_image"
    if getattr(tools, "IMAGE_GEN_AVAILABLE", False) else ""
) + (
    ", lsp_find_references, lsp_preview_rename, lsp_get_diagnostics"
    if getattr(tools, "LSP_TOOLS_AVAILABLE", False) else ""
) + (
    ", fetch_fetch, filesystem_read_file, filesystem_read_text_file, "
    "filesystem_list_directory, filesystem_directory_tree, "
    "filesystem_search_files, filesystem_get_file_info, filesystem_write_file"
    if getattr(tools, "MCP_AVAILABLE", False) else ""
) + (
    ", git_init, git_status, git_diff, git_commit, git_log, git_create_branch"
    if getattr(tools, "GIT_AVAILABLE", False) else ""
) + (
    ", rag_index_directory, rag_index_file, rag_search, rag_index_stats"
    if getattr(tools, "RAG_AVAILABLE", False) else ""
) + (
    ", ast_transform_var_to_const, ast_add_jsdoc, ast_find_untyped_functions"
    if getattr(tools, "AST_TOOLS_AVAILABLE", False) else ""
) + (
    ", render_ui, clear_ui"
    if getattr(tools, "A2UI_AVAILABLE", False) else ""
) + (
    ", dispatch_agent"
    if getattr(tools, "SUBAGENTS_AVAILABLE", False) else ""
) + """.

When given a task:
1. Discover first, don't guess. If you don't already know exactly what
   files exist, call list_files before calling read_file — never guess a
   filename and treat a "file not found" error as evidence the file doesn't
   exist. Use grep_files to locate where a function/class/symbol is defined
   or used before you try to edit it.
2. Plan your changes before acting.
3. Execute the plan using your tools (read_file, write_file, apply_edit,
   run_command). Very large files/command output are truncated with a note
   telling you how many lines/chars were cut — if you need a specific part
   of a large file, use grep_files to find it instead of trying to read
   the whole thing again.
   - For a SMALL, targeted change to an EXISTING file (fix one function,
     rename one variable, change a few lines), prefer apply_edit over
     write_file: give it old_string (copied EXACTLY from a real, recent
     read_file result -- never reconstructed from memory) and new_string.
     apply_edit fails closed (no write at all, with a clear error) if
     old_string is missing or appears more than once, instead of guessing
     where to apply a change or clobbering a file that changed since you
     last read it.
   - Use write_file for a brand-new file, or when the change is large
     enough (a full-function rewrite, most of the file) that there's no
     single short old_string worth anchoring an apply_edit to.
   - Never call apply_edit without having read_file'd the target in this
     same task first -- old_string must match current, real content
     exactly, including whitespace/indentation.
4. Verify the result — this step is mandatory whenever you use write_file
   or apply_edit, and "mentally tracing through the code" is NOT sufficient
   on its own:
   - Read the file back (read_file) to confirm the content is what you
     intended.
   - Then ACTUALLY EXECUTE it with run_command whenever the language/runtime
    allows it — e.g. for Python: `python -c "from mymodule import
     my_func; print(my_func(1, 2))"`, or run a test file/script directly.
     Look at the real output; do not just reason about what the output
     "should" be.
   - Only fall back to mental/manual tracing when execution is genuinely not
     possible (e.g. no interpreter available, requires external services).
     If you do this, say so explicitly in your final answer so the user
     knows the check wasn't run for real.
   - For HTML/CSS/JS work specifically""" + (
       ", use test_local_html to actually render the page in a real "
       "browser and screenshot it — reading the HTML/CSS source is NOT "
       "sufficient to confirm it looks/works correctly, since a file can "
       "be syntactically fine and still render broken (missing stylesheet "
       "link, wrong selectors, layout not matching the request, etc). "
       "test_local_html also reports any browser console errors, which "
       "you should treat as bugs to fix, not ignore. Use evaluate_js if "
       "you need to check something specific (element counts, a class "
       "being present) rather than eyeballing a screenshot description. "
       "If a task asks you to verify a SPECIFIC viewport size (e.g. "
       "'mobile, 375px width'), you MUST pass that exact width to "
       "test_local_html's width parameter and actually render at it — "
       "inferring from reading the CSS media queries that it 'should' work "
       "at that width is not verification and must not be reported as if "
       "it were. The tool's result will also flag if the page content "
       "overflows the requested width (real horizontal-scroll evidence). "
       "Use get_accessibility_snapshot when you need to know the exact "
       "structure/roles/text of interactive elements (headings, buttons, "
       "form fields, table cells) precisely -- e.g. to confirm a button's "
       "exact accessible name, or that a heading level is correct -- "
       "rather than inferring structure from raw HTML source or guessing "
       "from a screenshot description."
       if tools.BROWSER_TOOLS_AVAILABLE else
       " (no browser tools are available in this environment, so verify "
       "by reading the file back and reasoning carefully about correctness "
       "instead)."
   ) + """
   - If verification reveals a bug or mismatch, fix it with another
     write_file call and verify again — don't declare success until it
     actually checks out.
   - When you need to start a server/long-running process to verify against
     it (e.g. `flask run`, `python -m http.server`, `npm start`), use
     start_background_process, NOT run_command with a trailing '&' — the
     latter detaches with no PID you can track or stop later, which has
     caused real orphaned dev servers to survive past the task that started
     them. Once you're done verifying (curl'd it, screenshotted it, etc.),
     call stop_background_process with the handle it returned — don't leave
     it running. Use list_background_processes if you're unsure whether a
     server from earlier in this task is still up before starting another
     one on the same port.""" + (
    "\n- Use web_search when you need current/up-to-date information (recent "
    "library versions, current API syntax, something you're not confident "
    "about) rather than guessing from training data. Its result may note it "
    "fell back from DuckDuckGo to Tavily (or vice versa) -- that's normal, "
    "just use whatever results came back."
    if getattr(tools, "WEB_SEARCH_AVAILABLE", False) else ""
) + (
    "\n- Use generate_image for visual assets (banners, icons, hero images) "
    "instead of claiming you created one you didn't. Its result tells you "
    "the ACTUAL saved path/filename -- the real image format returned by "
    "the service doesn't always match the extension you requested, so "
    "always reference the exact path/filename the tool result gives you "
    "(e.g. in an <img src=...>), not the one you originally asked for."
    if getattr(tools, "IMAGE_GEN_AVAILABLE", False) else ""
) + (
    "\n- For renaming a function/variable used in more than one place, prefer "
    "lsp_find_references + lsp_preview_rename over grep_files + manual "
    "write_file edits -- they use real import/scope resolution and won't "
    "rename an unrelated symbol that just happens to share the name. "
    "lsp_preview_rename returns the COMPLETE new content for every affected "
    "file: pass that exact text to write_file verbatim, do not retype or "
    "reconstruct it yourself. After applying all the edits, run "
    "lsp_get_diagnostics on the changed files to confirm nothing broke -- "
    "but note its result will tell you explicitly if diagnostics aren't "
    "meaningful for that file type without extra project config (e.g. "
    "checkJs for plain JavaScript); in that case fall back to actually "
    "running the code (run_command) to verify instead of trusting a clean "
    "diagnostics result that may not mean anything."
    if getattr(tools, "LSP_TOOLS_AVAILABLE", False) else ""
) + (
    "\n- fetch_fetch and the filesystem_* tools come from external MCP "
    "(Model Context Protocol) servers, not this project's own code. Use "
    "fetch_fetch for web requests (it exists specifically so you never "
    "write custom HTTP code). For file operations, prefer the native "
    "read_file/write_file/list_files unless a task specifically calls for "
    "the MCP tools -- filesystem_write_file IS routed through the same "
    "write_file underneath (same confirmation/backup behavior) so it's "
    "safe to use, but there's no benefit to preferring it over the native "
    "tool for ordinary work. filesystem_read_file/list_directory/etc. are "
    "read-only and scoped to this project's test/ directory specifically "
    "(the MCP filesystem server's configured root), not the whole project."
    if getattr(tools, "MCP_AVAILABLE", False) else ""
) + (
    "\n- Use git_status/git_diff/git_commit/git_log/git_create_branch instead "
    "of raw run_command git calls -- they reuse the same sensitive-path "
    "detection as read_file/write_file, so a secret (.env, keys, "
    "credentials) can NEVER be committed or shown in a diff through these "
    "tools, with no override. git_init is NEVER called automatically by any "
    "other git_* tool -- only call it yourself if the user actually wants "
    "this project turned into a git repo (git_status will clearly say if it "
    "isn't one yet). git_create_branch refuses on a dirty working tree -- "
    "commit first if that's what's needed. Always check git_status before "
    "git_commit if you're not certain what's currently changed."
    if getattr(tools, "GIT_AVAILABLE", False) else ""
) + (
    "\n- Use rag_search for CONCEPT queries grep_files can't answer (e.g. "
    "'find where we handle authentication' when the code never uses that "
    "exact phrase) -- it's a complement to grep_files/lsp_find_references, "
    "not a replacement: use grep_files when you know the exact text/symbol, "
    "rag_search when you only know the concept. You MUST run "
    "rag_index_directory at least once before rag_search will return "
    "anything useful (it will say clearly if the index is empty). The "
    "index can go stale -- if you write_file a change to a file you plan "
    "to rag_search over later in the same task, call rag_index_file on it "
    "afterward so the index reflects the new content."
    if getattr(tools, "RAG_AVAILABLE", False) else ""
) + (
    "\n- ast_transform_var_to_const/ast_add_jsdoc/ast_find_untyped_functions "
    "are JavaScript-only surgical transforms beyond what lsp_preview_rename "
    "does. They return a PREVIEW of the full transformed file content, "
    "exactly like lsp_preview_rename -- you MUST call write_file yourself "
    "with that exact content to apply it; these tools never write anything "
    "on their own. ast_transform_var_to_const only converts a `var` to "
    "`const` if it's NEVER mutated anywhere in the file (checked via "
    "plain assignment, +=/-=/etc., AND ++/--) -- if it reports 'no changes "
    "needed', that's a correct, honest result, not a failure. After "
    "applying an AST transform with write_file, actually run the code "
    "(run_command) or use test_local_html/evaluate_js if it's browser-"
    "facing JS, to verify it still works -- the transform is syntax-aware "
    "but you should still confirm the specific result, the same way you "
    "verify any other write_file change."
    if getattr(tools, "AST_TOOLS_AVAILABLE", False) else ""
) + (
    "\n- render_ui/clear_ui show ADVISORY, purely informational hints in a "
    "webview (progress, diff previews, tool summaries, chat messages) -- "
    "they do NOT pause execution or represent user approval of anything. "
    "There is no CONFIRM_DIALOG template; sensitive/destructive actions "
    "are already gated automatically by the system itself, separately "
    "from anything you render here -- never use render_ui to imply "
    "permission was granted for an action."
    if getattr(tools, "A2UI_AVAILABLE", False) else ""
) + (
    "\n- dispatch_agent spawns a SEPARATE sub-agent with its own isolated "
    "tools/conversation -- you only see its final answer, none of its "
    "intermediate steps. Use it for genuinely independent research/"
    "exploration you want done without cluttering your own context (e.g. "
    "\"find every place authentication is checked across this large "
    "codebase\" as one delegated call instead of many greps/reads in your "
    "own loop) -- NOT for something you could just call one tool yourself "
    "to answer. subagent_type='explore' or 'plan' are READ-ONLY (cannot "
    "write/edit/run commands) -- if you need changes made, either do them "
    "yourself or use subagent_type='general-purpose'. Sub-agents are "
    "capped (a small number per task, and cannot nest further sub-agents) "
    "specifically because each one is a full extra multi-turn loop, not a "
    "single cheap call -- don't dispatch one for trivial lookups."
    if getattr(tools, "SUBAGENTS_AVAILABLE", False) else ""
) + (
    "\n- If an 'Available Skills' list appears above, call load_skill(name) "
    "when the current task matches one of those descriptions BEFORE starting "
    "work -- the skill's instructions will appear as that call's result, and "
    "you should follow them for the rest of the task. A skill's own frontmatter "
    "may mention tool hints (e.g. 'disallowed-tools') -- treat these as strong "
    "ADVISORY guidance from the skill's author, not an enforced restriction: "
    "nothing structurally prevents you from using a tool a skill advises "
    "against, but you should still follow that guidance unless the task "
    "genuinely requires otherwise. Use list_skills if you need to see exact "
    "skill names, or to check why an expected skill isn't in the Available "
    "Skills list (it will show parse errors for broken skills)."
    if getattr(tools, "SKILLS_AVAILABLE", False) else ""
) + (
    "\n- Custom project rules (an AGENTS.md at the project root, and/or files "
    "in .agent_rules/) may already be included above under 'Project Rules' -- "
    "those are ALWAYS-ON standing instructions (tech stack, conventions, style) "
    "and you should follow them without needing to be reminded. Some rules are "
    "PATH-SCOPED (only relevant to certain files) and are NOT shown upfront -- "
    "if one applies, it will appear automatically as a note attached to the "
    "read_file/write_file/apply_edit result for a matching file, at the moment "
    "you touch it. Use list_rules if you want to see every rule that exists "
    "(including path-scoped ones not yet shown) and their exact scope."
    if getattr(tools, "RULES_AVAILABLE", False) else ""
) + """

Efficiency:
- When you need to look at several files/searches that don't depend on each
  other's results (e.g. reading 3 unrelated files, or a few greps), request
  them as multiple tool calls in the SAME turn instead of one-at-a-time —
  they'll be run concurrently, which is faster. Only go one-at-a-time when a
  later call genuinely needs to see an earlier call's result first (e.g.
  read a file, THEN decide what to write based on its content).

Guidelines:
- Be concise but explain what you're doing and why.
- Prefer small, verifiable steps over large speculative changes.
- Never fabricate file contents, directory listings, or command output —
  always use a tool to check reality.
- write_file (and any tool that writes file content, e.g. filesystem_write_file)
  WRITES EXACTLY THE LITERAL TEXT YOU GIVE IT — it does not merge, patch,
  expand shorthand, or resolve a placeholder/reference on your behalf.
  NEVER pass a placeholder standing in for content you got from an earlier
  step, expecting the system to fill it in — it will be written literally,
  as that placeholder text itself. This applies to BOTH cases observed in
  practice:
    (a) a comment like "<!-- existing content -->" or "// rest unchanged"
        meant to stand in for a file's prior content, and
    (b) a tag like "<fetch_fetch_result>" or "[the weather data]" meant to
        stand in for an earlier tool call's actual output.
  In both cases: take the REAL text from the relevant observation (the
  file you read, or the tool result you received earlier in this
  conversation) and paste that exact text into the write_file call
  yourself — character for character, not a reference to it, not a
  variable name, not a summary of it.
- If you're modifying part of an existing file, read its full current
  content first (read_file), then write_file with the COMPLETE new content
  — the unchanged parts included verbatim, not referenced by a comment.
- After ANY write_file (new file or existing file), read_file it back and
  confirm the content is genuinely what you intended — both that
  unrelated parts weren't lost (for existing-file edits) AND that no
  placeholder/reference text (like a literal tag name) ended up in the
  file where real content should be. If you see a placeholder-looking
  string in the read-back, that IS the bug described above having
  happened — immediately re-write with the real content, don't just note
  the problem and move on.
- If something is ambiguous (e.g. you're not sure which file the user
  means, or a request could be interpreted multiple ways), ask the user
  for clarification instead of guessing. This is about AMBIGUOUS INTENT,
  not risk -- for destructive/irreversible commands specifically, do NOT
  ask about them in your chat reply instead of calling the tool. Just call
  run_command normally: the system itself automatically pauses and asks
  the human operator for real, mechanical confirmation before the command
  actually executes (see "Safety guardrails" below) -- that gate cannot be
  bypassed and does not need you to also ask in natural language first.
  Asking in chat INSTEAD OF calling the tool means the real confirmation
  gate never even sees the request, which defeats the entire mechanism.
- Once you're done, give a clear final summary of what changed and how it
  was verified. Do not call any more tools once you have your final answer.
- If a tool result gives you an actual filename/path (e.g. generate_image
  reporting it saved as .jpg instead of the .png you requested), use that
  EXACT actual value in any subsequent reference to it (e.g. <img src=...>)
  — do not use the name you originally asked for if the tool told you it's
  different. If verification (e.g. a console error) reveals that you did
  this wrong, fix it immediately — don't just mention the error and move on.

Safety guardrails (enforced by the tools themselves, not just this prompt):
- Secret/credential files (.env, private keys, git/AWS/SSH credentials, etc.)
  can never be read or written by you — read_file/write_file/run_command/
  grep_files will refuse or silently exclude them. Don't try to work around
  this; if a task seems to require touching such a file, tell the user why
  you can't.
- Destructive or irreversible shell commands (e.g. rm -rf, git reset --hard,
  force-push, DROP TABLE) will pause and ask the human operator for explicit
  confirmation before running, and may come back as "CANCELLED" if they
  decline. If that happens, do not retry the same command — report what was
  cancelled and why, and ask the user how they'd like to proceed.
- Overwriting an EXISTING file with different content (write_file) also
  pauses for human confirmation, showing a diff of exactly what would
  change. Creating a brand-new file, or writing identical content, does not
  need confirmation. If a write is cancelled, don't just retry it blindly —
  explain what you were trying to change and ask how to proceed.
- Every actual overwrite of an existing file is automatically backed up
  first, even if the confirmation was approved. If a write later turns out
  to be wrong, use undo_last_edit(path) to restore the file to its state
  from just before that write, rather than trying to manually reconstruct
  the old content from memory.
"""


def _default_confirm(name: str, args: dict, reason: str, diff: str | None = None) -> bool:
    """Default confirmation policy for main.py's interactive loop: ask the
    human at the terminal before running a flagged tool call. Prints to
    stderr and reads directly from stdin so it works even when the caller
    is capturing this function's return value. If `diff` is provided (an
    overwrite of an existing file), it's shown before asking."""
    print(f"\n⚠️  CONFIRMATION REQUIRED: {reason}", file=sys.stderr)
    print(f"    Tool: {name}({ {k: v for k, v in args.items() if k != 'content'} })", file=sys.stderr)
    if diff:
        print(f"\n{diff}\n", file=sys.stderr)
    try:
        answer = input("    Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    return answer in ("y", "yes")


# Tool names that ultimately write file content the same way write_file()
# does, and so need the exact same confirmation treatment. Found by direct
# testing that this needed to be explicit: filesystem_write_file (the
# MCP-namespaced write tool, routed through write_file() under the hood --
# see tools.mcp_write_file) was silently bypassing confirmation entirely,
# because _needs_confirmation only ever checked for the literal string
# "write_file" and had no idea a same-behavior tool could exist under a
# different name. Any future write-like tool (native or MCP-wrapped) must
# be added here explicitly -- there is deliberately no "looks like a write"
# heuristic, since guessing wrong in the permissive direction is exactly
# the failure mode this project has been bitten by before.
_WRITE_FILE_TOOL_NAMES = {"write_file", "filesystem_write_file"}

# apply_edit is a SEPARATE set, not merged into _WRITE_FILE_TOOL_NAMES,
# because its confirmation path needs a different diff computation
# (tools.diff_for_edit, keyed on old_string/new_string -- there is no
# `content` argument to feed tools.diff_for_write). Handled as its own
# branch in _needs_confirmation below rather than trying to force it
# through the write_file-shaped code path.
_APPLY_EDIT_TOOL_NAMES = {"apply_edit"}


def _needs_confirmation(name: str, args: dict) -> tuple[str, str | None] | None:
    """Return (reason, diff_or_None) if this tool call should be confirmed
    before running, or None if it's safe to run automatically.

    write_file (and anything in _WRITE_FILE_TOOL_NAMES) only needs
    confirmation when it would actually change an EXISTING file's content
    -- creating a brand-new file, or "overwriting" a file with byte-
    identical content, proceeds without friction. This keeps the safety
    net focused on the case that actually matters (losing real, different
    content) instead of nagging on every single write.

    apply_edit gets the same treatment via tools.diff_for_edit: if the
    edit can't be safely previewed (old_string missing/ambiguous, or the
    path is sensitive), no diff is shown here and the tool call proceeds
    to actually run -- where apply_edit() itself will return a clear
    ERROR (or, for a sensitive path, refuse outright) rather than silently
    doing nothing. The confirmation gate is a preview layer on top of the
    tool's own guardrails, not a replacement for them.
    """
    if name == "run_command" and tools.is_destructive_command(args.get("cmd", "")):
        return "This shell command looks destructive/irreversible.", None
    if name in _WRITE_FILE_TOOL_NAMES:
        path = args.get("path", "")
        if tools.is_sensitive_path(path):
            # Belt-and-suspenders: write_file() already hard-blocks this
            # itself, but surfacing it as a confirmable action too makes
            # the intent visible in logs/UIs that check this function.
            return "This would write to a path that looks like a secret/credentials file.", None
        diff = tools.diff_for_write(path, args.get("content", ""))
        if diff is not None:
            return "This would overwrite an existing file with different content.", diff
    if name in _APPLY_EDIT_TOOL_NAMES:
        path = args.get("path", "")
        if tools.is_sensitive_path(path):
            return "This would edit a path that looks like a secret/credentials file.", None
        diff = tools.diff_for_edit(path, args.get("old_string", ""), args.get("new_string", ""))
        if diff is not None:
            return "This would edit an existing file's content.", diff
    return None


def _dispatch_tool_call(
    name: str,
    arguments_json: str,
    confirm=None,
    cache: ToolCache | None = None,
    on_command_line=None,
    tool_functions: dict | None = None,
    subagent_depth: int = 0,
    subagent_budget=None,
) -> str:
    """Run a tool by name with JSON-encoded arguments, returning a string result.

    If the call matches a destructive/sensitive pattern (see tools.py's
    is_destructive_command / is_sensitive_path), `confirm(name, args, reason)`
    is called first; the tool only runs if it returns True. `confirm`
    defaults to an interactive terminal prompt (_default_confirm) but can be
    overridden (e.g. auto-approve for tests, auto-deny for CI, a GUI dialog).

    If `cache` is provided, read-only calls (read_file/list_files/grep_files)
    are served from it when the exact same call was already made earlier in
    this task, and any write_file/run_command/apply_edit call flushes it
    afterward — see cache.py for the full invalidation rationale.

    `on_command_line`, if given, is passed as run_command's `on_line`
    callback ONLY when the tool being dispatched is actually run_command --
    every other tool ignores it entirely. This is how a caller (e.g.
    bridge_server.py, for the VS Code webview) gets live, line-by-line
    output from a long-running command (npm install, pytest) instead of
    silence until it finishes. The model itself can never set this --
    run_command's TOOL_SPECS schema has no such parameter -- so this is
    purely an opt-in for trusted Python callers, layered on top of the
    exact same tools.run_command() the non-streaming path already uses;
    passing on_command_line=None (the default) reproduces the prior
    behavior exactly.

    `tool_functions`, if given, REPLACES the module-level TOOL_FUNCTIONS
    registry used to look up and call `name` -- this is the actual
    enforcement mechanism behind sub-agents (see subagents.py): a
    restricted sub-agent (e.g. "explore") is given a dict that structurally
    does not contain "write_file"/"apply_edit"/"run_command" at all, so a
    call to one of those tools fails with "ERROR: unknown tool" exactly the
    same way a genuinely nonexistent tool name would -- not a prompt-level
    instruction the model could ignore, an actual lookup failure. None (the
    default) reproduces the exact prior behavior of always using the full
    global TOOL_FUNCTIONS.

    `subagent_depth`/`subagent_budget`, if given, are forwarded ONLY to the
    "dispatch_agent" tool (see subagents.py) as extra keyword arguments
    invisible to the LLM's tool schema -- the same injection pattern
    already used for on_command_line above. Every other tool ignores them
    entirely.
    """
    confirm = confirm or _default_confirm
    funcs = tool_functions if tool_functions is not None else TOOL_FUNCTIONS

    if name not in funcs:
        return f"ERROR: unknown tool '{name}'"
    try:
        args = json.loads(arguments_json or "{}")
        if args is None:
            # Real bug found live while testing the new git_* tools (which
            # are the first zero/optional-arg tools common enough that a
            # model would plausibly call them with no arguments at all):
            # some providers emit the literal JSON string "null" for a
            # no-argument tool call rather than "{}". `json.loads("null" or
            # "{}")` returns Python None (truthy string "null" wins the
            # `or`), and TOOL_FUNCTIONS[name](**None) then crashes with an
            # unhandled-looking TypeError. Confirmed directly with a live
            # agent run against git_status/git_init. Treat a null-argument
            # payload the same as an empty one instead of letting it reach
            # the ** unpacking.
            args = {}
    except json.JSONDecodeError as e:
        return f"ERROR: could not parse arguments for {name}: {e}"

    # Plugin PreToolUse hooks (see plugins.py's module docstring, decision
    # 2): a REAL, additional deny gate, checked BEFORE this project's own
    # confirm() gate below -- a hook can block a call this project's own
    # _needs_confirmation would never have flagged at all (e.g. a plugin
    # wanting to veto a specific read_file path), not just relax/replace
    # the existing gate. Fail-open by construction (see plugins.run_hooks'
    # own docstring): a broken/slow/erroring hook never blocks anything,
    # it's simply skipped.
    if getattr(tools, "PLUGINS_AVAILABLE", False):
        try:
            import plugins as _plugins_module
            hook_decision = _plugins_module.run_hooks("PreToolUse", {"tool_name": name, "tool_args": args})
            if hook_decision and hook_decision.get("decision") == "block":
                return f"CANCELLED: blocked by a plugin PreToolUse hook ({hook_decision.get('reason', 'no reason given')})"
        except Exception:
            pass  # a broken plugins.py import/hook must never take down a real tool call

    confirmation = _needs_confirmation(name, args)
    if confirmation is not None:
        reason, diff = confirmation
        if not confirm(name, args, reason, diff):
            return f"CANCELLED: user declined to confirm this action ({reason})"

    def _call() -> str:
        try:
            call_args = dict(args)
            if name == "run_command" and on_command_line is not None:
                call_args["on_line"] = on_command_line
            if name == "dispatch_agent":
                # Same injection pattern as on_command_line above: these
                # are never part of dispatch_agent's TOOL_SPECS schema (the
                # LLM can't set or even see them), they're how the trusted
                # Python dispatch layer threads the confirm callback and
                # the shared depth/budget tracker into subagents.py without
                # exposing them as model-controllable arguments.
                call_args["_confirm"] = confirm
                call_args["_subagent_depth"] = subagent_depth
                call_args["_subagent_budget"] = subagent_budget
            return str(funcs[name](**call_args))
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:
            return f"ERROR: {name} raised an exception: {e}"

    if cache is None:
        return _call()

    result, was_cached = cache.get_or_call(name, args, _call)
    if was_cached:
        result = f"[cached result — file unchanged since last read this task]\n{result}"
    return result


def _sanitize_tool_calls(tool_calls) -> list[dict]:
    """
    Reduce each tool_call to the minimal, standard OpenAI shape:
        {"id": ..., "type": "function", "function": {"name": ..., "arguments": ...}}

    Why: tc.model_dump() includes whatever extra fields the *answering*
    provider's SDK attached (e.g. Groq adds "provider_specific_fields",
    an "index", etc.). That's harmless as long as the same provider keeps
    answering every turn — but our Router can fail over to a *different*
    provider mid-conversation, and some providers (observed: Cerebras)
    strictly validate incoming messages and reject unrecognized fields,
    crashing the whole loop with a 400 BadRequestError. Stripping down to
    the standard fields keeps conversation history provider-agnostic.
    """
    return [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            },
        }
        for tc in tool_calls
    ]


def _default_logger(event: str, payload: str) -> None:
    """Prints each ReAct step live, e.g. Thought / Action / Observation."""
    print(f"[{event}] {payload}")


def _build_messages(user_input: str, mem: dict, system_prompt: str | None = None) -> list[dict]:
    """Assemble the initial message list: system prompt + memory + new input.

    `system_prompt`, if given, is used instead of the module-level
    SYSTEM_PROMPT -- see run_agent's docstring (this is how a restricted
    sub-agent gets a narrower prompt describing only its own tools)."""
    messages = [{"role": "system", "content": system_prompt if system_prompt is not None else SYSTEM_PROMPT}]

    # Fold prior conversation history from memory into context.
    for turn in mem.get("history", []):
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": turn["assistant"]})

    # Optional running notes the agent has chosen to remember about the project.
    if mem.get("notes"):
        messages.append({
            "role": "system",
            "content": "Notes remembered from previous sessions:\n" + "\n".join(mem["notes"]),
        })

    messages.append({"role": "user", "content": user_input})
    return messages


def _is_batchable(tc) -> bool:
    """A tool call is safe to run concurrently with siblings from the same
    turn only if it's a known cacheable/read-only tool AND wouldn't need a
    confirm() prompt (confirmable actions must stay sequential so prompts
    don't interleave, and so a denial can't race with something else)."""
    if tc.function.name not in CACHEABLE_TOOLS:
        return False
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        return False  # let the sequential path surface the parse error normally
    return _needs_confirmation(tc.function.name, args) is None


# ---------------------------------------------------------------------------
# Batching nudge -- a real, measured engineering gap, not a hypothetical one.
#
# Found live during the furniture-site self-fix test (see README.md,
# "Honest gap-check" section): a real run made 4 CONSECUTIVE, fully
# independent read_file calls (App.jsx, App.css, main.jsx, index.css) as
# 4 separate LLM turns, even though _is_batchable/_run_tool_calls's
# ThreadPoolExecutor already exist specifically to run exactly this kind
# of call concurrently IN ONE TURN, and the system prompt's own
# "Efficiency:" section already tells the model to request them together.
# The machinery was correct; the model just didn't follow the advisory
# text. This section adds a CORRECTIVE OBSERVATION (not a new tool, not a
# new message role -- see the wire-format note below) that fires the
# moment a wasteful pattern is actually observed, not a hypothetical
# warning issued up front.
#
# Deliberately conservative about what counts as "independent" (false
# positives here just add a harmless one-line note; false negatives just
# mean a missed nudge -- but a WRONG nudge telling the model two
# genuinely-dependent calls "could have been batched" would be actively
# misleading):
#   - Both calls must individually satisfy _is_batchable -- reusing the
#     EXACT SAME eligibility check the real concurrent-execution path
#     uses, so "the nudge says these could batch" and "the engine would
#     actually run them concurrently if batched" can never disagree.
#   - Two list_files calls are NOT flagged as independent even if both are
#     individually batchable -- listing a parent directory and then a
#     subdirectory found in that listing is a common, genuinely SEQUENTIAL
#     discovery pattern (confirmed in the very same real transcript this
#     fix is based on: list_files("test/furniture_site") ->
#     list_files("test/furniture_site/src") -> list_files(".../dist") --
#     each one plausibly informed by the previous listing's contents).
#     Excluding this pair keeps the nudge from firing on a case where
#     "batch these" is often simply wrong advice.
#   - An exact repeat of the same (tool, args) is not flagged either --
#     cache.CACHEABLE_TOOLS already serves that from cache; there's
#     nothing to "batch instead," and duplicated-goal detection with a
#     "you already know this" wording is a different fix, not this one.
# ---------------------------------------------------------------------------

BATCHING_NUDGE_TEXT = (
    "Note: this and your previous tool call were both independent, read-only "
    "lookups (no result from one was needed to form the other's arguments). "
    "You could have requested them together as multiple tool_calls in the SAME "
    "turn to save a round trip -- see the system prompt's \"Efficiency\" section. "
    "This is not an error, just an efficiency reminder for future calls in this task."
)


def _solo_batchable_call_info(tool_calls) -> tuple[str, dict] | None:
    """If `tool_calls` (a turn's full list) is exactly one call AND that
    call is individually eligible for the existing concurrent-batch path
    (see _is_batchable), return (tool_name, parsed_args) for it; otherwise
    None. Used only to detect the specific "model made a single read-only
    call it could have batched with an adjacent turn's single call"
    pattern -- a genuine multi-call batched turn (len > 1) is exactly the
    GOOD behavior this nudge exists to encourage, so it deliberately
    returns None for those (nothing to correct)."""
    if len(tool_calls) != 1:
        return None
    tc = tool_calls[0]
    if not _is_batchable(tc):
        return None
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        return None
    return (tc.function.name, args)


def _should_nudge_to_batch(prev_call: tuple[str, dict] | None, curr_call: tuple[str, dict] | None) -> bool:
    """True if `curr_call` (this turn's lone batchable call) and
    `prev_call` (the previous turn's lone batchable call) look like a real,
    avoidable missed-batching opportunity -- both must be independently
    batchable (already guaranteed by how these tuples are built -- see
    _solo_batchable_call_info), and NOT the two conservative exclusions
    documented above (a list_files/list_files pair, or an exact repeat)."""
    if prev_call is None or curr_call is None:
        return False
    prev_name, prev_args = prev_call
    curr_name, curr_args = curr_call
    if prev_name == curr_name == "list_files":
        return False  # plausible parent->child directory discovery, not a real miss
    if prev_name == curr_name and prev_args == curr_args:
        return False  # exact repeat -- cache.py already serves this, nothing to batch
    return True


# ---------------------------------------------------------------------------
# Path-scoped rule injection -- see rules.py's module docstring for the
# full design (a rule with `paths:` frontmatter, matching real Cursor/
# Claude Code behavior, is injected only when a matching file is actually
# touched, not upfront). This extraction helper is the one piece of this
# feature that lives in agent.py rather than rules.py, because it needs
# agent.py's own _WRITE_FILE_TOOL_NAMES/_APPLY_EDIT_TOOL_NAMES sets (the
# single canonical definition of "this tool writes a file," already used
# by _needs_confirmation above) rather than rules.py re-declaring a
# second, parallel list that could drift out of sync.
_FILE_TOUCHING_TOOL_NAMES = _WRITE_FILE_TOOL_NAMES | _APPLY_EDIT_TOOL_NAMES | {
    "read_file", "filesystem_read_file", "filesystem_read_text_file",
}


def _touched_path_for_call(name: str, args: dict) -> str | None:
    """Returns the file path a tool call reads/writes, or None if `name`
    isn't a file-touching tool at all (or the path arg is missing/not a
    string) -- used to check path-scoped rules against, never used for any
    safety-relevant decision (that's tools.is_sensitive_path's job,
    unrelated to this feature)."""
    if name not in _FILE_TOUCHING_TOOL_NAMES:
        return None
    path = args.get("path")
    return path if isinstance(path, str) and path else None


def _run_tool_calls(
    tool_calls, confirm, cache, log, on_command_line=None,
    tool_functions: dict | None = None,
    subagent_depth: int = 0,
    subagent_budget=None,
) -> list[tuple[str, object]]:
    """
    Execute all tool calls from one LLM turn, returning [(result, tool_call), ...]
    in the SAME order as `tool_calls` (so conversation history stays aligned
    with each tool_call_id regardless of which finished first).

    Batchable calls (independent reads -- see _is_batchable) run concurrently
    in a thread pool; everything else runs sequentially, in order, before/
    after the batch as encountered. This mirrors how the model actually
    groups calls: if it interleaves a write between two reads, the write
    still runs in its correct position, not just "at the end".

    `on_command_line`, if given, is forwarded to _dispatch_tool_call so any
    run_command call in this turn streams its output live -- see
    _dispatch_tool_call's docstring for the full rationale. None by default,
    reproducing the exact prior (non-streaming) behavior.

    `tool_functions`/`subagent_depth`/`subagent_budget` are forwarded
    straight through to _dispatch_tool_call -- see that function's
    docstring. All default to values that reproduce the exact prior
    behavior (full global registry, depth 0, no budget object).
    """
    n = len(tool_calls)
    results: list[str | None] = [None] * n
    batch_indices = [i for i, tc in enumerate(tool_calls) if _is_batchable(tc)]

    def run_one(i: int) -> str:
        tc = tool_calls[i]
        args = tc.function.arguments or "{}"
        log("Action", f"{tc.function.name}({args})")
        result = _dispatch_tool_call(
            tc.function.name, args, confirm=confirm, cache=cache, on_command_line=on_command_line,
            tool_functions=tool_functions, subagent_depth=subagent_depth, subagent_budget=subagent_budget,
        )
        log("Observation", result)
        return result

    if len(batch_indices) > 1:
        # Real concurrency for the batchable subset (order-independent by
        # construction: read-only, no side effects to sequence).
        log("Action", f"[running {len(batch_indices)} independent read-only calls in parallel]")
        with ThreadPoolExecutor(max_workers=min(len(batch_indices), MAX_PARALLEL_TOOLS)) as pool:
            for i, result in zip(batch_indices, pool.map(run_one, batch_indices)):
                results[i] = result
        batch_set = set(batch_indices)
        # Everything else (non-batchable) still runs sequentially, in its
        # original position, interleaved correctly relative to the batch.
        for i in range(n):
            if i not in batch_set:
                results[i] = run_one(i)
    else:
        # 0 or 1 batchable calls -> no concurrency benefit, just run in order.
        for i in range(n):
            results[i] = run_one(i)

    return list(zip(results, tool_calls))


def run_agent(
    user_input: str,
    verbose: bool = True,
    log=None,
    confirm=None,
    mission_id: str | None = None,
    max_iterations: int | None = None,
    on_command_line=None,
    system_prompt: str | None = None,
    tool_functions: dict | None = None,
    tool_specs: list | None = None,
    persist_memory: bool = True,
    subagent_depth: int = 0,
    subagent_budget=None,
) -> str:
    """
    Run the ReAct loop for a single user message:

        Thought      -> the model decides what to do next
        Action       -> it calls a tool (read_file/write_file/run_command)
        Observation   -> the tool's result is fed back to the model
        ... repeat until the model has no more tool calls to make ...

    Uses persisted memory for cross-turn context and returns the agent's
    final natural-language reply.

    `confirm(name, args, reason) -> bool` gates destructive/sensitive tool
    calls (see tools.is_destructive_command / is_sensitive_path). Defaults
    to an interactive terminal y/N prompt; pass your own callable to
    auto-approve (tests), auto-deny (CI/dry-run), or hook up a UI.

    `mission_id`, if given, keeps this call's conversation history isolated
    in its own memory file (see missions.mission_memory_path) instead of the
    shared global memory.json. This is how a long task gets broken into
    missions (see missions.py) without any single mission's history ever
    needing to exceed memory.MAX_HISTORY_TURNS.

    `max_iterations`, if given, overrides MAX_TOOL_ITERATIONS for just this
    call -- useful for a task known in advance to need many tool calls
    (e.g. "set up a backend, verify 3 endpoints, then screenshot") without
    raising the global default for every simple task too.

    `on_command_line(line: str) -> None`, if given, is called for every
    line of output any run_command call in this task produces, AS IT
    ARRIVES -- e.g. so a long `npm install`/`pytest` shows live progress
    instead of a frozen screen for the whole duration. Purely additive:
    None (the default) reproduces the exact prior, non-streaming behavior.

    The remaining parameters exist specifically to support sub-agents (see
    subagents.py) and all default to values that reproduce run_agent's
    exact prior behavior when omitted:

    `system_prompt`, if given, REPLACES the module-level SYSTEM_PROMPT for
    just this call -- e.g. a restricted sub-agent gets a narrower prompt
    describing only the tools it actually has, instead of the full agent's
    prompt describing tools it structurally cannot call.

    `tool_functions`/`tool_specs`, if given, replace TOOL_FUNCTIONS/
    TOOL_SPECS for just this call -- this is the real, structural
    enforcement behind a restricted sub-agent (see _dispatch_tool_call's
    docstring): a tool absent from `tool_functions` cannot be called no
    matter what the model asks for, and absent from `tool_specs` means the
    model is never even told the tool exists.

    `persist_memory=False` skips loading/saving memory.json (or a mission's
    memory file) entirely -- the conversation starts from JUST
    `user_input` (plus `system_prompt`) and nothing is written to disk
    afterward. This is how a sub-agent gets a genuinely FRESH, ISOLATED
    history that never pollutes the parent's memory.json or leaks into a
    later unrelated task's context, instead of accidentally sharing
    (or worse, corrupting) the caller's own memory file.

    `subagent_depth`/`subagent_budget` are forwarded to any dispatch_agent
    tool call made during this run, so nested sub-agent spawning can be
    bounded (see subagents.py's MAX_SUBAGENT_DEPTH / SubagentBudget) --
    every other tool ignores them.
    """
    log = log or (_default_logger if verbose else (lambda event, payload: None))
    iteration_limit = max_iterations if max_iterations is not None else MAX_TOOL_ITERATIONS
    active_system_prompt = system_prompt if system_prompt is not None else SYSTEM_PROMPT
    active_tool_functions = tool_functions if tool_functions is not None else TOOL_FUNCTIONS
    active_tool_specs = tool_specs if tool_specs is not None else TOOL_SPECS

    # Skills' "Available Skills" metadata block (Layer 1 of progressive
    # disclosure -- see skills.py's module docstring) is deliberately
    # computed HERE, fresh on every run_agent() call, rather than baked
    # into the module-level SYSTEM_PROMPT string once at import time --
    # unlike every other optional-feature flag in SYSTEM_PROMPT (which
    # only depends on whether a PACKAGE is installed, fixed for the whole
    # process), skills are USER-AUTHORED FILES that can be added/edited on
    # disk between one run_agent() call and the next within the same
    # process (e.g. a user drops a new .agent_skills/my-skill/SKILL.md in
    # between two requests) -- a metadata block computed once at import
    # time would silently go stale. Appended to whatever system_prompt is
    # already in effect (the caller's own, if given, e.g. a restricted
    # sub-agent's narrower prompt -- otherwise the module-level default)
    # so a sub-agent still sees the same skill catalog the parent does,
    # rather than skills being silently invisible to every sub-agent.
    if getattr(tools, "SKILLS_AVAILABLE", False):
        try:
            import skills as _skills_module
            metadata_block = _skills_module.get_metadata_block(_skills_module.scan_skills())
            if metadata_block:
                active_system_prompt = f"{active_system_prompt}\n\n{metadata_block}"
        except Exception as e:
            # A skills-scanning failure must never take down the whole
            # task -- log it and proceed with the prompt as-is, exactly
            # the same defensive posture as skills.py's own per-skill
            # try/except in scan_skills() (see that module's docstring for
            # the real bug this general philosophy already fixed once).
            log("Error", f"Could not scan skills (proceeding without them): {type(e).__name__}: {e}")

    # Custom project rules (rules.py) -- an always-loaded root AGENTS.md
    # plus any .agent_rules/*.md file with no `paths:` frontmatter are
    # injected into the system prompt HERE, fresh per call, for the exact
    # same reason as skills' metadata block above (user-authored files
    # that can change on disk between calls). Path-scoped rules are
    # deliberately NOT injected here -- see path_scoped_rules below, which
    # feeds the mid-task injection point alongside the batching nudge.
    path_scoped_rules: list = []
    if getattr(tools, "RULES_AVAILABLE", False):
        try:
            import rules as _rules_module
            scanned_rules = _rules_module.scan_rules()
            always_loaded_block = _rules_module.get_always_loaded_block(scanned_rules)
            if always_loaded_block:
                active_system_prompt = f"{active_system_prompt}\n\n{always_loaded_block}"
            path_scoped_rules = _rules_module.get_path_scoped_rules(scanned_rules)
        except Exception as e:
            # Same defensive posture as the skills-scanning block above --
            # a rules-scanning failure must never take down the whole task.
            log("Error", f"Could not scan project rules (proceeding without them): {type(e).__name__}: {e}")

    # Plugin SessionStart hooks (see plugins.py's module docstring,
    # decision 2): CONTEXT ONLY, matching Claude Code's own documented
    # shape for this event exactly ("Context only... No blocking or
    # decision control") -- a hook may only append additional context to
    # the system prompt for this run, never block a session from starting.
    if getattr(tools, "PLUGINS_AVAILABLE", False):
        try:
            import plugins as _plugins_module_start
            hook_decision = _plugins_module_start.run_hooks("SessionStart", {"user_input": user_input})
            if hook_decision and hook_decision.get("context"):
                active_system_prompt = f"{active_system_prompt}\n\n[Plugin SessionStart context]\n{hook_decision['context']}"
        except Exception as e:
            log("Error", f"Could not run plugin SessionStart hooks (proceeding without them): {type(e).__name__}: {e}")

    if persist_memory:
        mem_path = missions.mission_memory_path(mission_id) if mission_id else None
        mem = memory.load(mem_path)
    else:
        mem_path = None
        mem = {"history": [], "notes": []}
    messages = _build_messages(user_input, mem, system_prompt=active_system_prompt)
    cache = ToolCache()  # fresh per task -- see cache.py for why it isn't shared across runs

    task_complete = False
    final_reply = ""
    step = 0
    # Tracks the (tool_name, args) of the PREVIOUS turn's lone batchable
    # call, across loop iterations, so _should_nudge_to_batch can compare
    # consecutive turns -- see the "Batching nudge" section above for why
    # this exists and what it's based on. None whenever the previous turn
    # wasn't a single solo batchable call (a multi-call turn, a write, a
    # confirmable call, or no tool call at all) -- there's nothing to
    # compare a new solo call against in those cases.
    prev_solo_batchable_call: tuple[str, dict] | None = None
    # Path-scoped rules (see rules.py) fire at most ONCE per rule per
    # task -- tracked by rule name, so a rule matching a file read/written
    # 10 times across a long task doesn't inject its full body 10 times,
    # bloating context with a duplicate of text the model already saw.
    fired_rule_names: set[str] = set()

    try:
        # ---- The ReAct loop -----------------------------------------------
        while not task_complete and step < iteration_limit:
            step += 1

            # Routed through LiteLLM: tries Groq first, then Gemini, Cerebras,
            # OpenRouter (whichever have keys set) until one succeeds.
            try:
                response = llm_client.chat_completion(messages=messages, tools=active_tool_specs)
            except llm_client.LLMTimeoutError as e:
                # Real, reproduced failure mode (diagnosed live with py-spy):
                # a provider (Groq) got put into a 2185s/36-minute cooldown
                # by litellm's Router, and because Router has no overall
                # deadline of its own, one chat_completion() call silently
                # blocked the whole ReAct loop for ~40 minutes before other
                # providers' short cooldowns expired. llm_client.chat_completion
                # now enforces a hard wall-clock budget (default 90s) and
                # raises LLMTimeoutError instead of hanging -- surface that
                # to the user honestly rather than let it look like a crash
                # or a silent stall.
                log("Error", f"LLM call timed out: {e}")
                final_reply = (
                    "I had to stop because every configured LLM provider was too slow "
                    "or rate-limited to respond within the timeout window "
                    f"({llm_client.DEFAULT_CHAT_TIMEOUT_SECONDS}s). This usually means a "
                    "provider hit a long rate-limit cooldown (minutes, not seconds). "
                    f"I completed {step - 1} step(s) before this happened. "
                    "Wait a bit and retry, or check which provider is rate-limited."
                )
                task_complete = True
                break
            except litellm.ContextWindowExceededError:
                # Individual tool outputs are capped (see tools.MAX_READ_FILE_CHARS
                # / MAX_TOOL_OUTPUT_CHARS), but accumulated conversation history
                # across many turns can still exceed a provider's context window
                # on a long-running task. Fail gracefully with a clear, honest
                # explanation instead of letting an unhandled exception crash the
                # whole process -- the user can start a fresh task (/reset) to
                # recover, rather than losing all output with a stack trace.
                log("Error", "Context window exceeded -- conversation grew too large for the model to process.")
                final_reply = (
                    "I had to stop because the conversation became too large for the model's "
                    "context window (this can happen on long, multi-file tasks). "
                    f"I completed {step - 1} step(s) before hitting this limit. "
                    "Try breaking the task into smaller pieces, or run /reset and start a "
                    "narrower request."
                )
                task_complete = True
                break

            if not response.choices:
                # Real bug found live while testing Gap 2 (background
                # process tracking): a provider/fallback path can return a
                # response with an EMPTY choices list (no error raised, so
                # none of the except blocks above catch it) -- previously
                # this crashed the whole mission with an unhandled
                # `IndexError: list index out of range` on
                # response.choices[0]. Treat it the same way as a real LLM
                # failure: log it, stop this loop honestly, and let the
                # caller decide whether to retry, rather than crashing.
                log("Error", "LLM returned a response with no choices (empty completion) -- treating as a failed turn.")
                final_reply = (
                    "I had to stop because the LLM provider returned an empty/invalid "
                    "response (no choices) instead of an error I could react to. "
                    f"I completed {step - 1} step(s) before this happened. "
                    "This is usually transient -- try again."
                )
                task_complete = True
                break
            choice = response.choices[0].message

            if choice.tool_calls:
                # Thought: the model may explain itself before/while calling a tool.
                if choice.content:
                    log("Thought", choice.content)

                messages.append({
                    "role": "assistant",
                    "content": choice.content,
                    "tool_calls": _sanitize_tool_calls(choice.tool_calls),
                })

                # Action + Observation: run every requested tool call, feed results back.
                #
                # A single LLM turn can request multiple tool calls at once (this
                # is native to OpenAI-style function calling, not a custom
                # format). When the model batches several *independent*,
                # read-only calls together (read_file/list_files/grep_files --
                # see cache.CACHEABLE_TOOLS), we run them concurrently in a
                # thread pool: they're I/O-bound (disk/subprocess), not
                # CPU-bound, so real wall-clock time is saved even under
                # Python's GIL. Anything else (write_file/run_command, or calls
                # needing a confirm() prompt) runs sequentially and in order --
                # both because they can have side effects / real dependencies on
                # each other, and to avoid interleaving multiple y/N prompts on
                # one terminal at once.
                #
                # Note: this reduces wall-clock latency per task, NOT the number
                # of LLM calls -- that's set by how many turns the model needs,
                # which depends on the task's actual dependency structure.
                # Batching-nudge bookkeeping (see the "Batching nudge"
                # section above): a real, measured gap -- the existing
                # concurrent-batch machinery and system-prompt guidance are
                # both advisory-only, and a real run made 4 consecutive
                # single-call turns that could have been 1. This turn's
                # solo-batchable-call info (if any) is computed BEFORE
                # dispatch so the comparison uses this turn's own request,
                # not anything about how it was executed.
                curr_solo_batchable_call = _solo_batchable_call_info(choice.tool_calls)
                should_nudge = _should_nudge_to_batch(prev_solo_batchable_call, curr_solo_batchable_call)

                results_this_turn = _run_tool_calls(
                    choice.tool_calls, confirm=confirm, cache=cache, log=log, on_command_line=on_command_line,
                    tool_functions=active_tool_functions, subagent_depth=subagent_depth, subagent_budget=subagent_budget,
                )
                for idx, (result, tc) in enumerate(results_this_turn):
                    if should_nudge and idx == len(results_this_turn) - 1:
                        # Appended to the CONTENT of the existing tool-result
                        # message, not a new message/role -- see
                        # _sanitize_tool_calls's own docstring for why this
                        # project keeps message shapes minimal/portable
                        # (a real, previously-found bug: Cerebras strictly
                        # validates incoming messages and rejects
                        # unrecognized fields when the Router fails over to
                        # it mid-conversation). A plain string append can
                        # never trigger that class of bug, on any provider.
                        result = f"{result}\n\n{BATCHING_NUDGE_TEXT}"
                        log("Note", BATCHING_NUDGE_TEXT)

                    # Path-scoped project rules (see rules.py): check the
                    # REQUESTED call's own name/args (not the result) --
                    # a rule should fire because the model TOUCHED a
                    # matching path, regardless of whether that touch
                    # succeeded or errored (e.g. a rule about test files
                    # should still surface even if the read failed because
                    # the file doesn't exist yet). Fires at most once per
                    # rule per task (fired_rule_names) -- reusing the
                    # SAME append-to-tool-result-content injection pattern
                    # as the batching nudge above, not a new mechanism.
                    if path_scoped_rules:
                        try:
                            call_args = json.loads(tc.function.arguments or "{}") or {}
                        except json.JSONDecodeError:
                            call_args = {}
                        touched_path = _touched_path_for_call(tc.function.name, call_args)
                        if touched_path:
                            # Local import (not at module top-level) matches
                            # every other optional-feature module's own
                            # lazy-import convention in this file (see the
                            # skills-scanning block above, which does the
                            # same for `import skills`) -- cheap after the
                            # first call since Python caches module imports.
                            import rules as _rules_module_for_matching
                            newly_matched = [
                                r for r in path_scoped_rules
                                if r.name not in fired_rule_names
                                and _rules_module_for_matching.rule_matches_path(r, touched_path)
                            ]
                            for rule in newly_matched:
                                fired_rule_names.add(rule.name)
                                rule_text = f"[Project rule '{rule.name}' applies to {touched_path}]\n{rule.body}"
                                result = f"{result}\n\n{rule_text}"
                                log("Note", rule_text)

                    # Plugin PostToolUse hooks (see plugins.py's module
                    # docstring, decision 2): same append-to-tool-result-
                    # content injection point as the batching nudge/rules
                    # above, not a new mechanism. A hook's {"decision":
                    # "block", "reason": ...} here is NOT a real undo (the
                    # tool already ran) -- it's a corrective observation
                    # appended for the model to react to on its next turn,
                    # the same "note, not a rewind" semantics rules.py's
                    # own path-scoped rules already established.
                    if getattr(tools, "PLUGINS_AVAILABLE", False):
                        try:
                            call_args = json.loads(tc.function.arguments or "{}") or {}
                        except json.JSONDecodeError:
                            call_args = {}
                        try:
                            import plugins as _plugins_module_post
                            hook_decision = _plugins_module_post.run_hooks(
                                "PostToolUse", {"tool_name": tc.function.name, "tool_args": call_args, "tool_result": result},
                            )
                            if hook_decision and hook_decision.get("decision") == "block":
                                hook_text = f"[Plugin PostToolUse hook note]\n{hook_decision.get('reason', '')}"
                                result = f"{result}\n\n{hook_text}"
                                log("Note", hook_text)
                        except Exception:
                            pass  # a broken plugin hook must never take down a real tool result

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                prev_solo_batchable_call = curr_solo_batchable_call
                # loop again: let the model react to the observation(s)

            else:
                # No tool calls requested -> the model is done. Task complete.
                final_reply = choice.content or ""
                task_complete = True

        if not task_complete:
            # Concise summary, not a raw dump of every tool observation --
            # found directly that the old version (joining every single
            # observation's full text) produced a "final reply" that could
            # be hundreds of lines long, including entire file contents
            # read/written along the way, making it hard to tell at a
            # glance what actually happened vs. just seeing repeated raw
            # tool output.
            tool_calls_made = [
                m for m in messages if m["role"] == "assistant" and m.get("tool_calls")
            ]
            action_summary = []
            for m in tool_calls_made:
                for tc in m["tool_calls"]:
                    action_summary.append(tc["function"]["name"])
            final_reply = (
                f"I stopped after {iteration_limit} steps without a final answer "
                f"(this task needed more tool calls than the current limit allows). "
                f"Tools called so far, in order: {', '.join(action_summary) if action_summary else '(none)'}. "
                "Any file writes/commands among these already happened for real -- "
                "check the actions above/on disk. To continue, re-run with a higher "
                "max_iterations, or ask a follow-up request to finish the remaining "
                "steps (e.g. via run_mission, which will resume from this point)."
            )

    except Exception as e:
        # CRITICAL: if the loop crashes for ANY reason we haven't already
        # handled above (rate limits, provider schema errors, network
        # failures -- all observed live during testing), don't let the
        # exception silently wipe out this turn. Any tool calls already run
        # (file writes, commands) already happened for real on disk; the
        # conversation record should say so, instead of the next session
        # starting from a memory.json that looks like nothing occurred.
        # Record what we know before re-raising, so the caller still sees
        # the original exception/traceback (this is not swallowed).
        partial_reply = (
            f"[INTERRUPTED before completion: {type(e).__name__}: {e}] "
            "Some tool calls in this turn may have already run for real "
            "(e.g. files may have been written) even though the task did not "
            "finish -- check the actions above/on disk rather than assuming "
            "nothing happened."
        )
        log("Error", partial_reply)
        if persist_memory:
            memory.append_turn(mem, user_input, partial_reply, mem_path)
        raise

    log("Cache", cache.stats())

    # Plugin Stop hooks (see plugins.py's module docstring, decision 2):
    # OBSERVATIONAL/ADVISORY ONLY -- a real Claude Code Stop hook can
    # force the agent to keep going; this project's version deliberately
    # does NOT re-enter the ReAct loop (see plugins.py for the full real-
    # scope-decision rationale). A hook's {"context": "..."} is appended
    # as a visible note on the final reply; the run always actually ends.
    if getattr(tools, "PLUGINS_AVAILABLE", False):
        try:
            import plugins as _plugins_module_stop
            hook_decision = _plugins_module_stop.run_hooks("Stop", {"user_input": user_input, "final_reply": final_reply})
            if hook_decision and hook_decision.get("context"):
                final_reply = f"{final_reply}\n\n[Plugin Stop hook note]\n{hook_decision['context']}"
        except Exception as e:
            log("Error", f"Could not run plugin Stop hooks (ignoring): {type(e).__name__}: {e}")

    if persist_memory:
        memory.append_turn(mem, user_input, final_reply, mem_path)
    return final_reply


def run_mission(
    user_input: str,
    mission_id: str,
    verbose: bool = True,
    log=None,
    confirm=None,
    max_iterations: int | None = None,
    cleanup_background_processes: bool = True,
    on_command_line=None,
    system_prompt: str | None = None,
    tool_functions: dict | None = None,
    tool_specs: list | None = None,
) -> str:
    """
    Like run_agent(), but scoped to a named mission (see missions.py):

      1. If this mission has a saved checkpoint from a PRIOR mission run
         (missions.load_progress), it's prepended to the user's request as
         context -- "here's what's already been done, what's next, and
         which files matter" -- instead of replaying the full prior
         conversation. This is what lets a long build stay broken into
         short, fresh-context missions rather than one unbounded chat.
      2. Runs the task via run_agent(..., mission_id=mission_id), so this
         mission's own turn-by-turn history stays in its own isolated
         memory file (never mixed with other missions, never silently
         evicting another mission's turns to make room).
      3. After the task completes, asks the model for a compact checkpoint
         (summary / next step / key files) and saves it via
         missions.save_progress -- so the NEXT mission (or a human) can
         pick up from here without needing this mission's raw transcript.

    Returns the mission's final reply (checkpoint-saving happens as a
    side effect and doesn't change what's returned).

    `system_prompt`/`tool_functions`/`tool_specs`, if given, are forwarded
    straight through to the underlying run_agent() call -- added
    specifically so permissions.run_mission_with_mode() can combine a
    permission mode (e.g. `plan`) with a resumed/named mission, exactly
    the same way permissions.run_agent_with_mode() already does for a
    plain (non-mission) run_agent() call. All default to None, reproducing
    this function's EXACT prior behavior (full global registry, module
    default system prompt) when omitted -- a real gap found and closed
    while adding session-resume CLI support: run_mission previously had
    no way to accept these at all, so --resume/--continue combined with
    --permission-mode would have silently ignored the mode.
    """
    log = log or (_default_logger if verbose else (lambda event, payload: None))

    prior = missions.load_progress(mission_id)
    if prior:
        log("Mission", f"Resuming '{mission_id}' from checkpoint saved at {prior['updated_at']}")
        context_preamble = (
            f"[Resuming mission '{mission_id}'. Prior progress checkpoint:]\n"
            f"Summary of what's done so far: {prior['summary']}\n"
            f"Planned next step (from last time): {prior['next_step']}\n"
            f"Key files: {', '.join(prior['key_files']) or '(none listed)'}\n\n"
            f"[New request for this mission:]\n{user_input}"
        )
    else:
        log("Mission", f"Starting new mission '{mission_id}'")
        context_preamble = user_input

    reply = run_agent(
        context_preamble, verbose=verbose, log=log, confirm=confirm,
        mission_id=mission_id, max_iterations=max_iterations,
        on_command_line=on_command_line,
        system_prompt=system_prompt, tool_functions=tool_functions, tool_specs=tool_specs,
    )

    # Ask the model for a compact, structured checkpoint of where things
    # stand now -- deliberately a SEPARATE, small request (not reusing the
    # full task's context) so this stays cheap and the summary itself can't
    # balloon the way an ever-growing transcript would.
    checkpoint_prompt = (
        f"Task just completed: {user_input}\n\n"
        f"Result: {reply}\n\n"
        "In 2-4 sentences, summarize what has now been accomplished in this "
        "mission overall (not just this one task). Then on a new line starting "
        "with 'NEXT:', state the single most useful next step for whoever "
        "continues this mission. Then on a new line starting with 'FILES:', "
        "list the key file paths involved so far, comma-separated (or 'none')."
    )
    try:
        checkpoint_reply = llm_client.chat_completion(
            messages=[{"role": "user", "content": checkpoint_prompt}],
        ).choices[0].message.content or ""
    except Exception as e:
        # Checkpointing is a nice-to-have on top of a successfully completed
        # task -- if the summarization call itself fails (rate limit, etc.),
        # don't let that mask the real result the user is waiting for.
        log("Error", f"Could not save mission checkpoint (task itself succeeded): {e}")
        return reply

    summary, next_step, key_files = checkpoint_reply, "", []
    for line in checkpoint_reply.splitlines():
        if line.strip().upper().startswith("NEXT:"):
            next_step = line.split(":", 1)[1].strip()
        elif line.strip().upper().startswith("FILES:"):
            files_part = line.split(":", 1)[1].strip()
            key_files = [f.strip() for f in files_part.split(",") if f.strip() and f.strip().lower() != "none"]
    # Whatever's left before the NEXT:/FILES: lines is the summary.
    summary_lines = [
        l for l in checkpoint_reply.splitlines()
        if not l.strip().upper().startswith("NEXT:") and not l.strip().upper().startswith("FILES:")
    ]
    summary = "\n".join(summary_lines).strip() or summary

    progress_path = missions.save_progress(mission_id, summary, next_step, key_files)
    log("Mission", f"Checkpoint saved to {progress_path}")

    # Real, previously reported bug: background processes started during a
    # mission (e.g. `flask run` for verification) survived past the mission
    # that started them, requiring manual pkill intervention at least twice
    # during the 3-mission stress test. A mission is a genuine "this unit of
    # work is now done" boundary -- unlike a single ad-hoc run_agent() call,
    # where a server might deliberately be left running for the next
    # request in the same session -- so auto-cleanup here is a safe default.
    # Override with cleanup_background_processes=False if a mission should
    # deliberately leave a server running for the user afterward.
    if cleanup_background_processes:
        still_tracked = process_manager.list_processes()
        if still_tracked:
            cleanup_msg = process_manager.cleanup_all()
            log("Mission", f"Cleaned up background process(es) left running at mission end:\n{cleanup_msg}")

    return reply
