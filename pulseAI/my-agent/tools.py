"""
tools.py
--------
The concrete capabilities the agent is allowed to use. Each tool is a plain
Python function plus a JSON-schema "spec" describing it to the LLM so the
model can decide when/how to call it (OpenAI-style function calling).

Keep this file boring and safe: no surprises, clear error handling, and every
tool returns a *string* (LLM messages are text) describing what happened.
"""

from __future__ import annotations

import difflib
import os
import re
import signal
import subprocess
import threading
from pathlib import Path

from checkpoint import CheckpointManager
import process_manager


def _kill_process_group(proc):
    """Kill a process and its whole process group, cross-platform.

    On Unix (start_new_session=True), the shell is its own group leader,
    so os.killpg terminates it AND all its children. On Windows,
    os.killpg does not exist, so we fall back to proc.kill().
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except AttributeError:
        proc.kill()       # Windows: no os.killpg
    except (ProcessLookupError, PermissionError):
        proc.kill()       # already gone or unsignalable


# Directory the agent is allowed to touch.
# type shenanigans by resolving everything relative to where the agent runs.
WORKDIR = Path.cwd()

# Deterministic, non-agent-controlled backup system for write_file (see
# checkpoint.py). One instance per process, so "undo the last edit" means
# "undo the last edit made during this run" -- exactly matching how the
# ReAct loop's own per-task state (e.g. ToolCache) is scoped.
checkpoint_mgr = CheckpointManager(WORKDIR)


def _resolve(path: str) -> Path:
    """Resolve a user-supplied path safely within WORKDIR."""
    p = (WORKDIR / path).resolve()
    if WORKDIR not in p.parents and p != WORKDIR:
        raise ValueError(f"Refusing to access path outside working directory: {path}")
    return p


# ---------------------------------------------------------------------------
# Safety guardrails
# ---------------------------------------------------------------------------
# Two independent concerns, checked at different layers:
#   1. SENSITIVE paths/commands (secrets: .env, private keys, git creds) are
#      HARD-BLOCKED here in tools.py itself, unconditionally. There is no
#      confirmation prompt that can override this — approving a leak doesn't
#      make it safe, since the content would be sent over the network to
#      whichever LLM provider answers this turn.
#   2. DESTRUCTIVE commands (rm -rf, DROP TABLE, force-push, etc.) are
#      flagged here via is_destructive_command() but NOT blocked in this
#      file — agent.py's dispatch layer uses that flag to require explicit
#      user confirmation before actually running them.

_SENSITIVE_TOKENS = (
    ".env", ".netrc", ".git-credentials", ".git/config", ".aws/credentials",
    ".ssh/", "id_rsa", "id_ed25519", "credentials.json", "secrets.json",
    "secrets.yaml", "secrets.yml",
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12")

# Conventional, deliberately-safe-to-commit template filenames that would
# otherwise false-positive on the ".env" substring check below (they exist
# specifically to be shared/committed, unlike the real .env they document).
_SAFE_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_sensitive_path(path: str) -> bool:
    """True if `path` looks like a local secret (.env, private keys, git/AWS
    credentials, etc.) that must never be read into the LLM's context or
    overwritten by it, regardless of user confirmation.

    Real false-positive found and fixed while testing git_tools.py against
    this project's own real files: `.env.example` (a deliberately blank,
    safe-to-commit template documented in README.md) was being flagged as
    sensitive purely because ".env" is a substring of ".env.example". Fixed
    by explicitly carving out conventional template suffixes -- but ONLY
    for the ".env" token specifically (not blanket-exempting anything that
    merely ends in ".example", since e.g. "id_rsa.example" should still be
    treated with suspicion -- this exemption is narrowly scoped to the one
    real, common pattern it's meant to fix).
    """
    normalized = str(path).replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]

    if name.startswith(".env") and any(name.endswith(suf) for suf in _SAFE_ENV_TEMPLATE_SUFFIXES):
        # e.g. ".env.example", ".env.sample" -- explicitly not sensitive,
        # even though ".env" is a substring, unless something else about
        # the path also matches (checked below via the normal token scan
        # continuing past this point for every OTHER token).
        remaining_tokens = tuple(t for t in _SENSITIVE_TOKENS if t != ".env")
        if any(tok in normalized for tok in remaining_tokens):
            return True
        if any(name.endswith(suf) for suf in _SENSITIVE_SUFFIXES):
            return True
        return False

    if any(tok in normalized for tok in _SENSITIVE_TOKENS):
        return True
    if any(name.endswith(suf) for suf in _SENSITIVE_SUFFIXES):
        return True
    return False


def references_sensitive_path(cmd: str) -> bool:
    """True if a shell command's text appears to reference a sensitive file
    (e.g. `cat .env`, `curl -F file=@.env ...`). Shell commands aren't path-
    restricted the way read_file/write_file are, so this catches attempts to
    exfiltrate secrets through run_command instead."""
    lowered = str(cmd).lower()
    return any(tok in lowered for tok in _SENSITIVE_TOKENS) or any(
        suf in lowered for suf in _SENSITIVE_SUFFIXES
    )


# Patterns for shell commands that are destructive/irreversible enough to
# warrant explicit human confirmation before running. Not exhaustive — this
# is a best-effort tripwire, not a sandbox — but it catches the common,
# obviously dangerous cases.
#
# Real gap found and fixed (via a competitor's own test scenario -- "rm
# old_auth.py should hit the confirmation gate" -- which failed against
# the ORIGINAL narrower pattern below): the original pattern only matched
# rm with an -rf/-fr-style recursive+force flag combination
# (r"\brm\s+-[a-z]*r[a-z]*f"). A plain `rm somefile.py` -- no flags at all
# -- matched NONE of these patterns and was therefore NEVER flagged as
# destructive, in ANY permission mode, including `default`. Confirmed
# directly: agent._needs_confirmation("run_command", {"cmd": "rm
# old_auth.py"}) returned None (meaning "runs completely unprompted")
# before this fix. Widened to r"\brm\s+" (any rm invocation at all) --
# run_command has no checkpoint/undo mechanism the way write_file/
# apply_edit do (see checkpoint.py), so ANY file deletion via rm is
# irreversible through this tool and warrants confirmation, not just the
# recursive-force-flag case. Confirmed no false-positive regression: word-
# boundary \b means this does NOT match "term", "germ", "confirm", etc.
# (only a real standalone "rm" token) -- reverified against the existing
# test suite (test/sensitive_path_test.py, test/permissions_test.py,
# test/confirm_bridge_test.py) which all still pass unchanged.
_DESTRUCTIVE_PATTERNS = (
    r"\brm\s+",                       # ANY rm invocation, not just -rf/-fr -- run_command has no undo
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{.*\|.*&.*\};",        # fork bomb
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+push\s+.*--force",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bdrop\s+table\b",
    r"\bdrop\s+database\b",
    r"\btruncate\s+table\b",
    r"\bdelete\s+from\b",
    r"\bchmod\s+-R\s+777\b",
    r">\s*/dev/sd[a-z]",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmv\s+.*\s+/dev/null",
)
_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_PATTERNS), re.IGNORECASE)


def is_destructive_command(cmd: str) -> bool:
    """True if `cmd` matches a known destructive/irreversible shell pattern
    (rm -rf, force-push, DROP TABLE, etc.) and should require explicit user
    confirmation before running."""
    return bool(_DESTRUCTIVE_RE.search(str(cmd)))



# ---------------------------------------------------------------------------
# Output size limits
# ---------------------------------------------------------------------------
# Every tool result becomes a message sent to the LLM, and providers reject
# the whole request once total conversation size exceeds their context
# window (confirmed live: an unbounded read_file() on a ~2.7MB/~670k-token
# file crashed the agent with "GroqException - Please reduce the length of
# the messages or completion", an unhandled BadRequestError). run_command
# and grep_files were already capped at 4000 chars; read_file previously had
# NO limit at all, which was the actual reproduced failure. All three now
# share a common, line-aware truncation helper so a single oversized tool
# result can never by itself blow the context budget -- it truncates
# instead, telling the model to use grep_files to search specific sections
# rather than silently crashing several turns later.
MAX_TOOL_OUTPUT_CHARS = 4000     # run_command / grep_files (unchanged)
MAX_READ_FILE_CHARS = 40000      # read_file gets more room -- it's often the primary content needed


def _truncate(text: str, max_chars: int, hint: str) -> str:
    """Truncate `text` to at most `max_chars`, cutting on a line boundary
    when possible so the result isn't sliced mid-line, and appending a note
    with `hint` for how to see the rest."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars * 0.5:  # only snap to a newline if it doesn't waste too much
        truncated = truncated[:last_newline]
    remaining_chars = len(text) - len(truncated)
    shown_lines = truncated.count("\n") + 1
    total_lines = text.count("\n") + 1
    return (
        f"{truncated}\n"
        f"... [truncated: showing {shown_lines} of {total_lines} lines, "
        f"{remaining_chars} more chars. {hint}]"
    )


def read_file(path: str) -> str:
    """Read and return the contents of a text file (truncated if very large --
    see MAX_READ_FILE_CHARS)."""
    try:
        if is_sensitive_path(path):
            return f"ERROR: refusing to read sensitive path '{path}' (secrets are never exposed to the LLM)."
        p = _resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        if not p.is_file():
            return f"ERROR: not a file: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        return _truncate(
            content, MAX_READ_FILE_CHARS,
            f"Use grep_files to search for specific functions/sections in {path} instead of reading it all at once.",
        )
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def diff_for_write(path: str, new_content: str) -> str | None:
    """
    Return a human-readable summary + unified diff if writing `new_content`
    to `path` would change an EXISTING file's content; return None if there
    is nothing to confirm -- either the file doesn't exist yet (a create,
    not an overwrite) or new_content is byte-identical to what's already
    there (a no-op write). agent.py's confirmation gate uses this to decide
    whether an overwrite needs a human's explicit approval.
    """
    try:
        if is_sensitive_path(path):
            return None  # write_file() hard-blocks this separately regardless
        p = _resolve(path)
    except Exception:
        return None
    if not p.exists() or not p.is_file():
        return None  # new file -- nothing to diff against
    try:
        old_content = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if old_content == new_content:
        return None  # no actual change -- don't bother asking

    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    # Diff without keepends -- files with no trailing newline would otherwise
    # produce a run-on line like "-old text+new text" with unified_diff's
    # keepends=True mode. join() below adds a clean "\n" between every line
    # regardless, at the minor cost of not preserving "\ No newline at end
    # of file" markers, which don't matter for a confirmation prompt.
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))

    MAX_DIFF_LINES = 40
    diff_text = "\n".join(diff_lines[:MAX_DIFF_LINES])
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_text += f"\n... ({len(diff_lines) - MAX_DIFF_LINES} more diff lines not shown)"

    summary = (
        f"Overwrite existing file '{path}': "
        f"{len(old_lines)} -> {len(new_lines)} lines, "
        f"{len(old_content)} -> {len(new_content)} chars.\n\n"
    )
    return summary + diff_text


def write_file(path: str, content: str) -> str:
    """Write (overwrite) a text file with the given content, creating parent dirs.

    Before overwriting any EXISTING file, this unconditionally saves a
    timestamped backup via checkpoint_mgr -- regardless of how the write
    was approved (or if no approval was needed, e.g. a brand-new file has
    nothing to back up). This is deterministic, not something the LLM can
    skip or influence: even a mistaken confirmation is always recoverable
    with the undo_last_edit tool. See checkpoint.py for the full rationale.
    """
    try:
        if is_sensitive_path(path):
            return f"ERROR: refusing to write to sensitive path '{path}' (secrets are never touched by this agent)."
        p = _resolve(path)
        backup_path = checkpoint_mgr.checkpoint_before_write(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if backup_path:
            return (
                f"OK: wrote {len(content)} chars to {path} "
                f"(backup saved as {backup_path.name} -- use undo_last_edit to revert this write)"
            )
        return f"OK: wrote {len(content)} chars to {path} (new file, nothing to back up)"
    except Exception as e:
        return f"ERROR writing {path}: {e}"


def diff_for_edit(path: str, old_string: str, new_string: str) -> str | None:
    """
    Preview-only: compute the unified diff apply_edit() would produce,
    WITHOUT touching the file. Returns None whenever a confirmable diff
    can't be safely shown -- sensitive path, missing file, old_string not
    found, or old_string not unique -- mirroring diff_for_write's
    "None means nothing to confirm" contract so agent.py's confirmation
    gate can treat both the same way.

    Real bug found and fixed here before this function ever shipped: a
    first draft (matching an externally proposed design) skipped the
    is_sensitive_path check entirely, on the theory that "it just returns
    a diff, it doesn't write anything." Confirmed directly that this
    doesn't matter -- returning a diff of a sensitive file's contents to
    the caller (and from there, into the LLM's context / a UI) is exactly
    the kind of leak read_file/write_file/grep_files already refuse for.
    Diffing is reading; it needs the same guardrail.
    """
    try:
        if is_sensitive_path(path):
            return None
        p = _resolve(path)
    except Exception:
        return None
    if not p.exists() or not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if content.count(old_string) != 1:
        return None  # missing or ambiguous -- apply_edit itself will explain why
    new_content = content.replace(old_string, new_string, 1)
    return diff_for_write(path, new_content)


def apply_edit(path: str, old_string: str, new_string: str) -> str:
    """
    Surgically edit an EXISTING file: replace exactly one occurrence of
    old_string with new_string. Fails closed (no write at all) if
    old_string is missing or appears more than once -- so a file that
    changed since it was last read simply won't match, instead of the
    edit landing in the wrong place. Complements write_file, which stays
    the right tool for new files or rewrites large enough that there's no
    single old_string worth anchoring to.

    Same safety net as write_file: is_sensitive_path is checked (never
    editable, no matter how narrow the change), and every real write goes
    through checkpoint_mgr.checkpoint_before_write first, so undo_last_edit
    works identically regardless of whether the last change came from
    write_file or apply_edit.
    """
    try:
        if is_sensitive_path(path):
            return f"ERROR: refusing to edit sensitive path '{path}' (secrets are never touched by this agent)."
        p = _resolve(path)
    except Exception as e:
        return f"ERROR: invalid path {path}: {e}"

    if not p.exists() or not p.is_file():
        return f"ERROR: file not found: {path}. Use write_file to create a new file."

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR reading {path}: {e}"

    occurrences = content.count(old_string)

    if occurrences == 0:
        return (
            f"ERROR: old_string not found in {path}. The file may have changed since "
            "you last read it, or old_string doesn't match exactly (check whitespace/"
            "indentation) -- read_file it again before retrying."
        )

    if occurrences > 1:
        contexts = []
        start = 0
        shown = min(occurrences, 3)
        for i in range(shown):
            idx = content.find(old_string, start)
            ctx_start = max(0, idx - 25)
            ctx_end = min(len(content), idx + len(old_string) + 25)
            contexts.append(f"  Match {i + 1}: ...{content[ctx_start:ctx_end]}...")
            start = idx + len(old_string)
        if occurrences > 3:
            contexts.append(f"  ... and {occurrences - 3} more")
        return (
            f"ERROR: old_string appears {occurrences} times in {path} -- it must be "
            f"unique. Include more surrounding context to disambiguate.\n" + "\n".join(contexts)
        )

    new_content = content.replace(old_string, new_string, 1)

    backup_path = checkpoint_mgr.checkpoint_before_write(p)
    p.write_text(new_content, encoding="utf-8")

    line_delta = new_content.count("\n") - content.count("\n")
    backup_note = (
        f"backup saved as {backup_path.name} -- use undo_last_edit to revert this write"
        if backup_path else "nothing to back up"
    )
    return (
        f"OK: edited {path} ({len(old_string)}->{len(new_string)} chars, "
        f"{line_delta:+d} lines) ({backup_note})"
    )


def undo_last_edit(path: str) -> str:
    """Restore `path` to the content it had immediately before the most
    recent write_file call against it during this session. Fails clearly if
    there's no such checkpoint (e.g. the file was never written this
    session, or has already been undone once)."""
    try:
        if is_sensitive_path(path):
            return f"ERROR: refusing to touch sensitive path '{path}'."
        p = _resolve(path)
        success, msg = checkpoint_mgr.undo_last_write(p)
        return f"{'OK' if success else 'ERROR'}: {msg}"
    except Exception as e:
        return f"ERROR undoing edit to {path}: {e}"


def run_command(cmd: str, timeout: int = 30, on_line=None) -> str:
    """Run a shell command and return combined stdout/stderr (truncated).

    `on_line` is an OPTIONAL callback (line: str) -> None, called once per
    output line AS IT ARRIVES, before the command finishes -- lets a
    caller (e.g. bridge_server.py, for the VS Code webview) show live
    progress on a long-running command (`npm install`, `pytest`) instead
    of a frozen screen for up to `timeout` seconds. Purely additive: with
    on_line=None (the default -- what the LLM's own tool call always uses,
    since on_line isn't in TOOL_SPECS' parameters and can't be set by the
    model), behavior is IDENTICAL to before this was added -- same
    blocking subprocess.run()-equivalent semantics, same return value,
    same truncation. The streaming path is a strict superset used only by
    trusted Python callers, never something the LLM can influence.

    Real bug avoided here, found by checking this project's own
    architecture before writing this: an earlier proposed design used
    `fcntl`+`select` directly on a `text=True` Popen pipe to poll for
    output non-blockingly. Confirmed that pattern is fragile (partial-line
    reads, BlockingIOError vs IOError inconsistency across platforms/
    buffering modes) and unnecessary -- a plain background thread doing
    blocking `readline()` calls and pushing to a queue (the same pattern
    bridge_server.py already uses successfully for its own stdout reader)
    is simpler and more robust, and doesn't require any non-portable
    fcntl flag manipulation.
    """
    try:
        if references_sensitive_path(cmd):
            return f"ERROR: refusing to run a command that references a sensitive file: {cmd}"

        if on_line is None:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout or "") + (result.stderr or "")
            output = output.strip() or "(no output)"
            output = _truncate(output, MAX_TOOL_OUTPUT_CHARS, "Re-run with a more targeted command (e.g. pipe through grep/head) to see specific parts.")
            return f"exit_code={result.returncode}\n{output}"

        # Streaming path: a background thread does blocking readline()
        # calls (real, portable blocking I/O -- no fcntl/select needed)
        # and calls on_line() for each line as it arrives; the main thread
        # just waits for the process to exit or the timeout to elapse.
        #
        # start_new_session=True is required for the timeout/kill path to
        # actually work: confirmed directly that killing just the shell
        # process (proc.kill()) does NOT close the stdout pipe if the
        # shell's own child (e.g. `sleep 5` inside `cmd`) is still running
        # and still holds the pipe's write end open -- the reader thread's
        # blocking readline() then hangs until that grandchild eventually
        # exits on its own (measured: ~4.7s extra wait for a `sleep 5`
        # after killing just the shell), defeating the whole point of a
        # short timeout. start_new_session puts the shell in its own
        # process group so os.killpg can terminate the shell AND every
        # process it spawned together -- confirmed this closes the pipe
        # immediately (readline() returns '' in <1ms instead of ~4.7s).
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=WORKDIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        collected: list[str] = []

        def _reader() -> None:
            try:
                for line in proc.stdout:  # blocks until a line is available or EOF
                    stripped = line.rstrip("\n")
                    collected.append(stripped)
                    try:
                        on_line(stripped)
                    except Exception:
                        pass  # a broken UI callback must never kill the command it's watching
            except Exception:
                pass

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.wait()
            reader_thread.join(timeout=2)
            output = "\n".join(collected).strip() or "(no output)"
            output = _truncate(output, MAX_TOOL_OUTPUT_CHARS, "Re-run with a more targeted command to see specific parts.")
            return f"ERROR: command timed out after {timeout}s\nPartial output before timeout:\n{output}"

        # Process exited -- give the reader thread a moment to drain any
        # remaining buffered lines (EOF on the pipe stops its for-loop).
        reader_thread.join(timeout=2)
        output = "\n".join(collected).strip() or "(no output)"
        output = _truncate(output, MAX_TOOL_OUTPUT_CHARS, "Re-run with a more targeted command (e.g. pipe through grep/head) to see specific parts.")
        return f"exit_code={returncode}\n{output}"

    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR running command: {e}"


def start_background_process(cmd: str, name: str = "") -> str:
    """Start a long-running command (dev server, watcher, etc.) in the
    background and TRACK it so it can be listed/stopped later via
    list_background_processes/stop_background_process, instead of leaking
    past the current task the way a plain `run_command "... &"` would.

    Real bug this replaces: run_command's shell `&` backgrounding gives the
    caller no PID at all (the shell detaches the child without reporting
    it back), so there was never anything to track or clean up -- confirmed
    directly that a Flask dev server started that way survived past the
    task/mission that launched it and required a manual pkill. Use THIS
    tool for anything you intend to keep running (servers), and plain
    run_command only for commands that finish on their own.
    """
    if references_sensitive_path(cmd):
        return f"ERROR: refusing to run a command that references a sensitive file: {cmd}"
    try:
        result = process_manager.start(cmd, cwd=WORKDIR, name=name or cmd[:60])
        return (
            f"Started background process '{name or cmd[:60]}' "
            f"(handle={result['handle']}, pid={result['pid']}). "
            "Use stop_background_process(handle) to shut it down when you're "
            "done verifying against it -- don't leave it running unnecessarily."
        )
    except Exception as e:
        return f"ERROR starting background process: {e}"


def stop_background_process(handle: str) -> str:
    """Stop a background process previously started with
    start_background_process, given the handle it returned. Always call
    this once you're done using a background server (e.g. after taking a
    screenshot / running curl checks against it) -- it will not stop
    itself."""
    try:
        return process_manager.stop(handle)
    except Exception as e:
        return f"ERROR stopping background process {handle}: {e}"


def list_background_processes() -> str:
    """List currently tracked background processes (handle, pid, command,
    how long they've been running). Use this if you're unsure whether a
    server you started earlier in this conversation is still up before
    starting another one on the same port."""
    try:
        procs = process_manager.list_processes()
        if not procs:
            return "(no tracked background processes running)"
        import time as _time
        lines = []
        for handle, info in procs.items():
            age = _time.time() - info.get("start_time", _time.time())
            lines.append(
                f"handle={handle} pid={info['pid']} name={info.get('name','?')} "
                f"age={age:.0f}s cmd={info['cmd']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR listing background processes: {e}"


def list_files(directory: str = ".") -> str:
    """List files and subdirectories directly inside a directory, so the
    agent can see what actually exists before guessing paths to read_file."""
    try:
        p = _resolve(directory)
        if not p.exists():
            return f"ERROR: directory not found: {directory}"
        if not p.is_dir():
            return f"ERROR: not a directory: {directory}"
        entries = sorted(os.listdir(p))
        if not entries:
            return f"(empty directory: {directory})"
        labeled = [
            f"{name}/" if (p / name).is_dir() else name
            for name in entries
        ]
        return "\n".join(labeled)
    except Exception as e:
        return f"ERROR listing {directory}: {e}"


def grep_files(pattern: str, directory: str = ".") -> str:
    """Search for a text pattern across files in a directory (like `grep -rn`),
    so the agent can find where a function/symbol is defined before editing it.
    Sensitive files (.env, credentials, keys, etc.) are excluded from the
    search entirely -- see is_sensitive_path -- so a match inside one can
    never leak its content back into the LLM's context via this tool."""
    try:
        p = _resolve(directory)
        if not p.exists():
            return f"ERROR: directory not found: {directory}"
        result = subprocess.run(
            [
                "grep", "-rn",
                # Exclude sensitive filenames/dirs outright so their content
                # is never even scanned, let alone returned in a match line.
                "--exclude=.env*", "--exclude=*.pem", "--exclude=*.key",
                "--exclude=*.pfx", "--exclude=*.p12", "--exclude=.netrc",
                "--exclude=.git-credentials", "--exclude=credentials.json",
                "--exclude=secrets.json", "--exclude=secrets.yaml", "--exclude=secrets.yml",
                "--exclude-dir=.ssh", "--exclude-dir=.git",
                "--", pattern, str(p),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # grep exit code 1 just means "no matches found", not an error.
        if result.returncode not in (0, 1):
            return f"ERROR: grep failed (exit {result.returncode}): {result.stderr.strip()}"
        output = result.stdout.strip()
        # Belt-and-suspenders: even with --exclude, defensively drop any
        # line whose file path still looks sensitive (e.g. a differently
        # named credentials file the --exclude list didn't anticipate).
        if output:
            kept_lines = [
                line for line in output.splitlines()
                if not is_sensitive_path(line.split(":", 1)[0])
            ]
            output = "\n".join(kept_lines)
        if not output:
            return f"(no matches for {pattern!r} in {directory})"
        # Make paths relative to WORKDIR for readability, and cap output size.
        output = output.replace(str(WORKDIR) + "/", "")
        output = _truncate(output, MAX_TOOL_OUTPUT_CHARS, "Use a more specific pattern to narrow down the results.")
        return output
    except subprocess.TimeoutExpired:
        return "ERROR: grep timed out after 15s"
    except Exception as e:
        return f"ERROR grepping {directory}: {e}"


# Map of tool name -> callable, used by the agent loop to dispatch calls.
TOOL_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "apply_edit": apply_edit,
    "run_command": run_command,
    "list_files": list_files,
    "grep_files": grep_files,
    "undo_last_edit": undo_last_edit,
    "start_background_process": start_background_process,
    "stop_background_process": stop_background_process,
    "list_background_processes": list_background_processes,
}

# JSON-schema descriptions of the tools, in OpenAI "tools" format.
# These get sent to the LLM so it knows what it can call and with what args.
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text contents of a file at a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file to read.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file, or completely rewrite an existing one, with the given "
                "text content. For a small, targeted change to an EXISTING file (fixing "
                "one function, renaming one variable, changing a few lines), prefer "
                "apply_edit instead -- it's safer (fails if the file changed since you "
                "read it) and produces a much clearer diff than a full-file rewrite."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_edit",
            "description": (
                "Surgically edit an EXISTING file by replacing exactly ONE occurrence of "
                "old_string with new_string. old_string must match the file's current "
                "content EXACTLY (including whitespace/indentation) and must be unique -- "
                "if it's missing or appears more than once, this fails with no write at "
                "all, telling you why, rather than guessing. You MUST read_file the "
                "target first so old_string is copied from real, current content, not "
                "reconstructed from memory. Use write_file instead for a brand-new file "
                "or a rewrite large enough that there's no single anchoring string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the existing file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact existing text to replace -- must be unique in the file, matching current content exactly.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text. Can be an empty string to delete old_string outright.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command and return its stdout/stderr and exit code. "
                "Do NOT use this to start a long-running server/process with a trailing "
                "'&' -- use start_background_process instead, which actually tracks the "
                "PID so it can be cleanly stopped later. A '&'-backgrounded command here "
                "will detach with no way for you to stop it afterward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to allow the command to run (default 30).",
                    },
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_background_process",
            "description": (
                "Start a long-running command (dev server, watcher, etc.) in the "
                "background with real PID tracking, so it can be listed/stopped later. "
                "Use this instead of run_command with a trailing '&' for anything you "
                "intend to keep running, like `flask run` or `python -m http.server`. "
                "ALWAYS call stop_background_process when you're done using it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "The shell command to run in the background (e.g. 'python app.py').",
                    },
                    "name": {
                        "type": "string",
                        "description": "A short human-readable label for this process (e.g. 'flask-dashboard').",
                    },
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_background_process",
            "description": (
                "Stop a background process previously started with start_background_process, "
                "given the handle it returned. Call this once you're done verifying against "
                "a server you started -- it will not stop on its own."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "The handle returned by start_background_process.",
                    },
                },
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_background_processes",
            "description": (
                "List currently tracked background processes (handle, pid, command, "
                "how long each has been running). Use this to check if a server you "
                "started earlier is still up before starting another one on the same port."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and subdirectories directly inside a directory. "
                "Use this FIRST to discover what actually exists before guessing "
                "filenames to read_file — directories are shown with a trailing '/'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory to list, relative to the project root. Defaults to '.'.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": (
                "Recursively search for a text pattern across files in a directory "
                "(like `grep -rn`). Use this to find where a function, class, or "
                "symbol is defined/used before editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search in, relative to the project root. Defaults to '.'.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "undo_last_edit",
            "description": (
                "Restore a file to the content it had immediately before the most "
                "recent write_file call against it in this session. Use this if a "
                "write_file result turns out to be wrong and you need to revert it "
                "before trying again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file to restore.",
                    }
                },
                "required": ["path"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Optional browser tools (screenshot / visual verification / JS evaluation)
# ---------------------------------------------------------------------------
# Only registered if Playwright + a working headless Chromium are actually
# usable in this environment -- confirmed by direct testing that this needs
# more than `pip install playwright`: also `playwright install chromium`
# and (on Linux) `sudo playwright install-deps chromium` for OS-level shared
# libraries (headless Chromium failed to even launch without them). Rather
# than expose tools that would always error, they're only added here if the
# import + basic availability check in tools_browser.py succeeds -- so an
# environment without them just doesn't offer them to the LLM at all.
try:
    from tools_browser import BROWSER_TOOLS_AVAILABLE, BROWSER_TOOL_FUNCTIONS, BROWSER_TOOL_SPECS
except Exception:
    BROWSER_TOOLS_AVAILABLE = False
    BROWSER_TOOL_FUNCTIONS = {}
    BROWSER_TOOL_SPECS = []

if BROWSER_TOOLS_AVAILABLE:
    TOOL_FUNCTIONS.update(BROWSER_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(BROWSER_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Optional web search + image generation tools
# ---------------------------------------------------------------------------
# Same "only register if actually usable" pattern as the browser tools.
# web_search works with zero setup if the `ddgs` package is installed (it
# has its own internal DuckDuckGo -> Tavily fallback -- see tools_web.py);
# generate_image needs the `requests` package (already a transitive
# dependency here) and has no API key requirement (Pollinations.ai is a
# free, keyless endpoint) but can fail at call-time if it's rate-limited --
# that's handled inside tools_image.py itself, not by hiding the tool.
try:
    from tools_web import TOOL_FUNCTIONS as _WEB_TOOL_FUNCTIONS, TOOL_SPECS as _WEB_TOOL_SPECS
    WEB_SEARCH_AVAILABLE = True
except Exception:
    _WEB_TOOL_FUNCTIONS, _WEB_TOOL_SPECS = {}, []
    WEB_SEARCH_AVAILABLE = False

try:
    from tools_image import TOOL_FUNCTIONS as _IMAGE_TOOL_FUNCTIONS, TOOL_SPECS as _IMAGE_TOOL_SPECS
    IMAGE_GEN_AVAILABLE = True
except Exception:
    _IMAGE_TOOL_FUNCTIONS, _IMAGE_TOOL_SPECS = {}, []
    IMAGE_GEN_AVAILABLE = False

if WEB_SEARCH_AVAILABLE:
    TOOL_FUNCTIONS.update(_WEB_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_WEB_TOOL_SPECS)

if IMAGE_GEN_AVAILABLE:
    TOOL_FUNCTIONS.update(_IMAGE_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_IMAGE_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Optional LSP (semantic code understanding) tools
# ---------------------------------------------------------------------------
# Only registered if pylspclient is importable -- individual language
# servers (pylsp / typescript-language-server) are checked lazily at call
# time instead, since which ones are installed can vary per-project and
# failing there gives a much more specific, actionable error message than
# hiding the whole tool category over one missing language server.
try:
    from tools_lsp import TOOL_FUNCTIONS as _LSP_TOOL_FUNCTIONS, TOOL_SPECS as _LSP_TOOL_SPECS
    LSP_TOOLS_AVAILABLE = True
except Exception:
    _LSP_TOOL_FUNCTIONS, _LSP_TOOL_SPECS = {}, []
    LSP_TOOLS_AVAILABLE = False

if LSP_TOOLS_AVAILABLE:
    TOOL_FUNCTIONS.update(_LSP_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_LSP_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Optional MCP (Model Context Protocol) tools
# ---------------------------------------------------------------------------
# Connects to external MCP servers (filesystem, fetch) and exposes their
# tools alongside the native ones. Two things are handled specially here,
# not just passed through naively:
#
# 1. SAFETY GAP: the MCP filesystem server's own write_file/edit_file
#    bypass this project's entire existing safety net (diff-before-
#    overwrite confirmation, automatic backup via checkpoint_mgr,
#    undo_last_edit) if registered as raw pass-throughs -- they'd write
#    straight to disk through a completely separate code path. Rather than
#    exposing those two MCP tools directly, they're intercepted below and
#    redirected through THIS project's own write_file()/diff_for_write(),
#    so an MCP-driven write gets exactly the same confirmation/backup
#    behavior as a native one. Every other MCP filesystem tool (read_file,
#    list_directory, search_files, etc.) is read-only and passed through
#    directly -- there's nothing to protect there.
#
# 2. LAZY CONNECTION: servers are only actually connected (spawning the
#    npx/mcp-server-fetch subprocesses) the first time an MCP tool is
#    used, not at import time -- importing tools.py happens even for
#    simple non-MCP tasks, and there's no reason to pay the ~2-3s
#    npx-download-and-start cost (confirmed directly) for every agent run.
try:
    import mcp_client as _mcp_client
    MCP_AVAILABLE = _mcp_client.MCP_AVAILABLE
except Exception:
    _mcp_client = None
    MCP_AVAILABLE = False

_mcp_connected = False


def _ensure_mcp_connected() -> str | None:
    """Connect to MCP servers on first real use. Returns an error string if
    connection failed outright, else None. A rare double-connect race is
    harmless -- connect_all_sync() is itself idempotent (skips servers
    already in mcp_manager.sessions)."""
    global _mcp_connected
    if not MCP_AVAILABLE:
        return "MCP SDK not available."
    if not _mcp_connected:
        _mcp_client.connect_all_sync()
        _mcp_connected = True
    return None


def mcp_call(tool_name: str, **kwargs) -> str:
    """
    Generic dispatcher for MCP tools, registered under their real namespaced
    name (e.g. 'fetch_fetch', 'filesystem_read_file') in TOOL_FUNCTIONS.
    filesystem_write_file / filesystem_edit_file are intentionally NOT
    reachable through here -- see mcp_write_file below for why.
    """
    err = _ensure_mcp_connected()
    if err:
        return f"ERROR: {err}"
    if tool_name not in _mcp_client.mcp_manager.tool_map:
        return (
            f"ERROR: MCP tool '{tool_name}' is not connected. "
            f"{_mcp_client.get_connection_status()}"
        )
    return _mcp_client.call_tool_sync(tool_name, kwargs)


def mcp_write_file(path: str, content: str) -> str:
    """
    Write a file via the MCP filesystem server's PROTOCOL name, but through
    this project's own write_file() implementation underneath -- so it gets
    the same diff-before-overwrite confirmation, automatic backup, and
    undo_last_edit support as every other write in this project, instead of
    bypassing that safety net via a separate MCP code path. Functionally
    equivalent to just calling write_file(path, content) directly; this
    wrapper exists so the LLM can use the MCP-namespaced tool name it
    discovered from the filesystem server without landing on an unguarded
    write path.
    """
    return write_file(path, content)


def _register_mcp_tools() -> None:
    """Register MCP tool specs, WITHOUT connecting yet (see
    _ensure_mcp_connected for why connection is deferred). Registers a
    fixed, known-in-advance set of specs for the two servers this project
    configures (filesystem, fetch) -- matching mcp_client.py's
    connect_all_sync(). If a server fails to connect at call time, the
    tool call itself reports that clearly rather than the tool having
    silently not existed."""
    TOOL_FUNCTIONS["fetch_fetch"] = lambda **kw: mcp_call("fetch_fetch", **kw)
    TOOL_SPECS.append({
        "type": "function",
        "function": {
            "name": "fetch_fetch",
            "description": (
                "[MCP:fetch] Fetch a URL from the internet and return its content "
                "(simplified to markdown when possible). Use this for web requests "
                "instead of writing custom HTTP code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch."},
                    "max_length": {"type": "integer", "description": "Max characters to return. Defaults to 5000."},
                    "start_index": {"type": "integer", "description": "Character offset to resume from if a previous fetch was truncated."},
                    "raw": {"type": "boolean", "description": "Return raw HTML instead of simplified markdown."},
                },
                "required": ["url"],
            },
        },
    })

    # Read-only filesystem tools: passed straight through to the MCP server.
    _READONLY_FS_TOOLS = {
        "filesystem_read_file": "Read a file's contents via the MCP filesystem server.",
        "filesystem_read_text_file": "Read a text file's contents via the MCP filesystem server.",
        "filesystem_list_directory": "List a directory's contents via the MCP filesystem server.",
        "filesystem_directory_tree": "Get a recursive directory tree via the MCP filesystem server.",
        "filesystem_search_files": "Search for files by name pattern via the MCP filesystem server.",
        "filesystem_get_file_info": "Get metadata (size, dates, permissions) for a file via the MCP filesystem server.",
    }
    def _make_mcp_dispatcher(tool_name: str):
        # Factory function so each dispatcher closes over its OWN tool_name
        # (a lambda referencing the loop variable `name` directly would have
        # every dispatcher call the LAST tool registered -- the classic
        # late-binding closure bug).
        return lambda **kw: mcp_call(tool_name, **kw)

    for name, desc in _READONLY_FS_TOOLS.items():
        TOOL_FUNCTIONS[name] = _make_mcp_dispatcher(name)
        TOOL_SPECS.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (
                    f"[MCP:filesystem] {desc} Note: this project also has a native "
                    "read_file/list_files with the same effect and its own "
                    "path-safety checks -- prefer those unless you specifically "
                    "need this MCP tool's exact behavior."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Path relative to the MCP filesystem server's root (this project's test/ directory)."}},
                    "required": ["path"],
                },
            },
        })

    # write_file / edit_file: registered under their MCP names, but routed
    # through this project's OWN write_file() -- see mcp_write_file above.
    TOOL_FUNCTIONS["filesystem_write_file"] = lambda **kw: mcp_write_file(**kw)
    TOOL_SPECS.append({
        "type": "function",
        "function": {
            "name": "filesystem_write_file",
            "description": (
                "[MCP:filesystem, SAFETY-WRAPPED] Write a file. Routed through this "
                "project's own write_file (same diff-confirmation, backup, and "
                "undo_last_edit support as the native tool) rather than the MCP "
                "server's raw write path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root."},
                    "content": {"type": "string", "description": "Full content to write."},
                },
                "required": ["path", "content"],
            },
        },
    })


if MCP_AVAILABLE:
    _register_mcp_tools()

# ---------------------------------------------------------------------------
# Optional git tools (semantic, safety-checked git operations)
# ---------------------------------------------------------------------------
# Replaces raw `run_command("git ...")` for anything commit/branch-related
# with structured operations that reuse THIS module's own is_sensitive_path
# check (git_tools.py imports tools as _tools and calls _tools.is_sensitive_path
# directly) -- rather than a separate, narrower secret-detection list that
# could silently drift out of sync with the real one. Only registered if
# GitPython is importable; git_init is a deliberately separate, explicit
# tool -- never invoked automatically as a side effect of any other call.
try:
    from git_tools import GIT_AVAILABLE, TOOL_FUNCTIONS as _GIT_TOOL_FUNCTIONS, TOOL_SPECS as _GIT_TOOL_SPECS
except Exception:
    GIT_AVAILABLE = False
    _GIT_TOOL_FUNCTIONS, _GIT_TOOL_SPECS = {}, []

if GIT_AVAILABLE:
    TOOL_FUNCTIONS.update(_GIT_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_GIT_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Optional RAG (semantic code search) tools
# ---------------------------------------------------------------------------
# Answers "find where we handle X" concept queries that neither grep_files
# (exact text) nor lsp_find_references (exact symbol) can. Only registered
# if chromadb is importable. Uses chromadb's own small default ONNX
# embedder (NOT sentence-transformers+torch, which doesn't fit this
# sandbox's /tmp -- see rag_indexer.py's module docstring for the direct
# reproduction of why that swap was necessary).
try:
    from rag_indexer import RAG_AVAILABLE, TOOL_FUNCTIONS as _RAG_TOOL_FUNCTIONS, TOOL_SPECS as _RAG_TOOL_SPECS
except Exception:
    RAG_AVAILABLE = False
    _RAG_TOOL_FUNCTIONS, _RAG_TOOL_SPECS = {}, []

if RAG_AVAILABLE:
    TOOL_FUNCTIONS.update(_RAG_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_RAG_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Optional AST (Tree-sitter) surgical code transform tools
# ---------------------------------------------------------------------------
# Beyond LSP's rename/reference tools (tools_lsp.py): custom, rule-based
# transforms like "convert every safely-convertible var to const" or "find
# functions missing JSDoc". Only registered if tree-sitter + the JS grammar
# are importable. Unlike git_tools.py/rag_indexer.py, ast_tools.py itself
# takes/returns plain strings and never imports `tools` at all (not even
# lazily) -- these wrapper functions live HERE, in tools.py, specifically
# so path resolution/sensitive-path checks/file I/O go through this
# module's own, already-tested _resolve/is_sensitive_path/read_file rather
# than being duplicated in ast_tools.py.
try:
    from ast_tools import AST_TOOLS_AVAILABLE as _AST_TOOLS_AVAILABLE
except Exception:
    _AST_TOOLS_AVAILABLE = False

AST_TOOLS_AVAILABLE = _AST_TOOLS_AVAILABLE

if AST_TOOLS_AVAILABLE:
    import ast_tools as _ast_tools

    def ast_transform_var_to_const(file_path: str) -> str:
        """Read a JS file, convert every safely-convertible `var` to
        `const` (never a var that's mutated anywhere via =, +=/-=/etc., or
        ++/--), and return the FULL transformed content -- same
        "read-only preview, caller applies via write_file" pattern as
        lsp_preview_rename. Never writes anything itself."""
        try:
            if is_sensitive_path(file_path):
                return f"ERROR: refusing to read a sensitive path: {file_path}"
            p = _resolve(file_path)
            if not p.exists() or not p.is_file():
                return f"ERROR: file not found: {file_path}"
            source = p.read_text(errors="ignore")
            result = _ast_tools.transform_var_to_const_safe(source)
            if result == source:
                return f"No changes needed -- either no `var` declarations found, or all are mutated somewhere and must stay `var`."
            return (
                f"Transformed content for {file_path} (this is a PREVIEW -- "
                f"call write_file with this exact content to apply it):\n\n{result}"
            )
        except Exception as e:
            return f"ERROR transforming {file_path}: {e}"

    def ast_add_jsdoc(file_path: str, function_name: str, params: dict, returns: str = "void") -> str:
        """Read a JS file, insert a JSDoc comment before the named
        top-level function, and return the FULL transformed content (same
        preview-only pattern as ast_transform_var_to_const)."""
        try:
            if is_sensitive_path(file_path):
                return f"ERROR: refusing to read a sensitive path: {file_path}"
            p = _resolve(file_path)
            if not p.exists() or not p.is_file():
                return f"ERROR: file not found: {file_path}"
            source = p.read_text(errors="ignore")
            result = _ast_tools.add_jsdoc_to_function(source, function_name, params, returns)
            return (
                f"Transformed content for {file_path} (this is a PREVIEW -- "
                f"call write_file with this exact content to apply it):\n\n{result}"
            )
        except ValueError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR transforming {file_path}: {e}"

    def ast_find_untyped_functions(file_path: str) -> str:
        """Find top-level JS functions with no JSDoc comment immediately
        above them (heuristic, not a strict linter -- see
        ast_tools.find_untyped_functions's docstring)."""
        try:
            if is_sensitive_path(file_path):
                return f"ERROR: refusing to read a sensitive path: {file_path}"
            p = _resolve(file_path)
            if not p.exists() or not p.is_file():
                return f"ERROR: file not found: {file_path}"
            source = p.read_text(errors="ignore")
            funcs = _ast_tools.find_untyped_functions(source)
            if not funcs:
                return "No untyped (undocumented) functions found."
            return "\n".join(f"- {f['name']} (line {f['line']}): {f['params']}" for f in funcs)
        except Exception as e:
            return f"ERROR analyzing {file_path}: {e}"

    TOOL_FUNCTIONS.update({
        "ast_transform_var_to_const": ast_transform_var_to_const,
        "ast_add_jsdoc": ast_add_jsdoc,
        "ast_find_untyped_functions": ast_find_untyped_functions,
    })
    TOOL_SPECS.extend([
        {
            "type": "function",
            "function": {
                "name": "ast_transform_var_to_const",
                "description": (
                    "Preview converting `var` to `const` in a JavaScript file, ONLY for "
                    "variables never mutated anywhere in the file (via =, +=/-=/etc., or "
                    "++/--). Returns the full transformed content as a PREVIEW -- you must "
                    "call write_file yourself with this exact content to apply it; this "
                    "tool never writes anything."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the JavaScript file."},
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ast_add_jsdoc",
                "description": (
                    "Preview inserting a JSDoc comment before a named top-level JavaScript "
                    "function. Returns the full transformed content as a PREVIEW -- call "
                    "write_file yourself with this exact content to apply it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the JavaScript file."},
                        "function_name": {"type": "string", "description": "Exact name of the top-level function to document."},
                        "params": {
                            "type": "object",
                            "description": "Map of parameter name to type string, e.g. {\"amount\": \"number\"}.",
                        },
                        "returns": {"type": "string", "description": "Return type string (default 'void')."},
                    },
                    "required": ["file_path", "function_name", "params"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ast_find_untyped_functions",
                "description": (
                    "Find top-level JavaScript functions with no JSDoc comment immediately "
                    "above them -- a heuristic to find documentation candidates, not a "
                    "strict linter (a JSDoc separated from the function by a blank line "
                    "won't be detected as present)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the JavaScript file."},
                    },
                    "required": ["file_path"],
                },
            },
        },
    ])

# ---------------------------------------------------------------------------
# Optional A2UI (advisory webview UI hints) tools
# ---------------------------------------------------------------------------
# Purely informational display for a future webview/extension host --
# progress, diff previews, tool-result summaries, chat-style messages.
# Deliberately NOT a confirmation mechanism: real, blocking approval gates
# are handled by confirm_bridge.py, wired directly into agent.py's own
# `confirm` callback parameter, not exposed as an LLM-callable tool the
# model could call for show without it actually gating anything. See
# a2ui.py's module docstring for the full reasoning -- a prior proposed
# design conflated "show a confirmation-looking card" with "actually pause
# execution pending approval", which would have been a real safety
# regression if built as originally specified.
try:
    from a2ui import A2UI_AVAILABLE, TOOL_FUNCTIONS as _A2UI_TOOL_FUNCTIONS, TOOL_SPECS as _A2UI_TOOL_SPECS
except Exception:
    A2UI_AVAILABLE = False
    _A2UI_TOOL_FUNCTIONS, _A2UI_TOOL_SPECS = {}, []

if A2UI_AVAILABLE:
    TOOL_FUNCTIONS.update(_A2UI_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_A2UI_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Sub-agents (dispatch_agent)
# ---------------------------------------------------------------------------
# Lets the main agent delegate a self-contained sub-task to a separate,
# restricted ReAct loop (see subagents.py for the full design: real
# structural tool-registry restriction per subagent_type, depth/budget
# limits given this project's rate-limit-prone free-tier provider stack).
# subagents.py itself only imports `tools`/`agent` LAZILY inside its
# functions (never at module level) specifically so this import here, at
# the very end of tools.py after TOOL_FUNCTIONS/TOOL_SPECS already exist,
# can never hit the same circular-import class of bug documented above for
# git_tools.py/rag_indexer.py.
try:
    from subagents import TOOL_FUNCTIONS as _SUBAGENT_TOOL_FUNCTIONS, TOOL_SPECS as _SUBAGENT_TOOL_SPECS
    SUBAGENTS_AVAILABLE = True
except Exception:
    SUBAGENTS_AVAILABLE = False
    _SUBAGENT_TOOL_FUNCTIONS, _SUBAGENT_TOOL_SPECS = {}, []

if SUBAGENTS_AVAILABLE:
    TOOL_FUNCTIONS.update(_SUBAGENT_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_SUBAGENT_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Agent Skills (load_skill / list_skills)
# ---------------------------------------------------------------------------
# Reusable, on-demand instruction bundles the model loads mid-task via a
# real tool call, per Anthropic's officially published spec
# (code.claude.com/docs/en/skills) -- see skills.py's own module docstring
# for the full design, including a real bug found and fixed (one malformed
# SKILL.md could otherwise crash the whole registry) and a deliberate
# scope decision (tools_hint is advisory-only in v1, not a hard
# restriction -- the real enforcement path for a task that needs it is
# dispatch_agent, which already restricts tools at spawn time). Only
# registered if PyYAML is importable (already present transitively via
# chromadb's own requirements, but never hard-required here). skills.py
# itself only imports `tools` LAZILY (never at module level), matching
# git_tools.py/rag_indexer.py/subagents.py's own established pattern for
# avoiding the exact circular-import class of bug documented above.
try:
    from skills import SKILLS_AVAILABLE, TOOL_FUNCTIONS as _SKILLS_TOOL_FUNCTIONS, TOOL_SPECS as _SKILLS_TOOL_SPECS
except Exception:
    SKILLS_AVAILABLE = False
    _SKILLS_TOOL_FUNCTIONS, _SKILLS_TOOL_SPECS = {}, []

if SKILLS_AVAILABLE:
    TOOL_FUNCTIONS.update(_SKILLS_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_SKILLS_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Custom Project Rules (list_rules)
# ---------------------------------------------------------------------------
# User-authored, version-controlled standing instructions -- an always-
# loaded root AGENTS.md (a genuinely open, cross-tool standard) plus
# .agent_rules/*.md files, some always-loaded and some path-scoped (only
# injected when the agent touches a matching file, reusing the exact
# corrective-observation mechanism already proven in agent.py's batching
# nudge). See rules.py's own module docstring for the full design,
# including a real stdlib bug found and fixed (pathlib.PurePosixPath.match
# does NOT implement correct globstar "**" semantics -- glob.translate
# does, verified directly against 8 test cases). Reuses skills.py's exact
# frontmatter-parsing primitive (parse_frontmatter) rather than
# duplicating it. Only registered if PyYAML is importable (same
# dependency, same availability flag as skills.py). rules.py itself only
# imports `tools` LAZILY, matching every other optional module's
# established circular-import-avoidance pattern.
try:
    from rules import RULES_AVAILABLE, TOOL_FUNCTIONS as _RULES_TOOL_FUNCTIONS, TOOL_SPECS as _RULES_TOOL_SPECS
except Exception:
    RULES_AVAILABLE = False
    _RULES_TOOL_FUNCTIONS, _RULES_TOOL_SPECS = {}, []

if RULES_AVAILABLE:
    TOOL_FUNCTIONS.update(_RULES_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_RULES_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Repo Map (repo_map_query)
# ---------------------------------------------------------------------------
# A PageRank-ranked map of the codebase's most structurally important
# files (definitions + real import graph), following the algorithm made
# popular by Aider (Apache-2.0, independently-open, safe to build from)
# and described (without implementation) in OpenClaude's own public docs.
# See repo_map.py's own module docstring for the full fact-check writeup,
# including a real bug in a proposal's own tree-sitter verification
# snippet (parser.set_language(...) doesn't exist in the actually-
# installed tree-sitter 0.26.0), a real JS require()-detection query bug
# (matched every function call, not just require()), and a deliberate
# decision to hand-roll PageRank rather than rely on networkx (present in
# this sandbox only by coincidence via an unrelated package, not a real
# project dependency -- see REPO_MAP_NETWORKX_UPGRADE.md for the
# documented future upgrade path). repo_map.py itself only imports
# `tools` LAZILY, matching every other optional module's established
# circular-import-avoidance pattern.
try:
    from repo_map import REPO_MAP_AVAILABLE, TOOL_FUNCTIONS as _REPO_MAP_TOOL_FUNCTIONS, TOOL_SPECS as _REPO_MAP_TOOL_SPECS
except Exception:
    REPO_MAP_AVAILABLE = False
    _REPO_MAP_TOOL_FUNCTIONS, _REPO_MAP_TOOL_SPECS = {}, []

if REPO_MAP_AVAILABLE:
    TOOL_FUNCTIONS.update(_REPO_MAP_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_REPO_MAP_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Custom Agent Definitions (list_custom_agents)
# ---------------------------------------------------------------------------
# User-authored, version-controlled `.agent_agents/*.md` files that
# pre-configure a dispatch_agent(agent_name=...) call: a fixed system
# prompt, an optional restricted tool set, an optional permission mode,
# and optional single-parent inheritance (extends:) for composing a
# specialized agent out of a shared base. See custom_agents.py's own
# module docstring for the full design, including 3 explicit decisions
# made where a proposal for this feature left things ambiguous (no
# model: field in v1 -- llm_client.chat_completion hardcodes
# model=first_model with no per-call override hook; skills: is metadata-
# only, never force-preloaded, matching skills.py's own already-shipped
# design; tools: composes with mode: via INTERSECTION, never union) and a
# real circular-import bug (subagents<->permissions) confirmed live and
# avoided by keeping that import lazy, the same established pattern every
# other optional module here already uses. custom_agents.py itself only
# imports `tools` LAZILY, matching that same pattern.
try:
    from custom_agents import (
        CUSTOM_AGENTS_AVAILABLE,
        TOOL_FUNCTIONS as _CUSTOM_AGENTS_TOOL_FUNCTIONS,
        TOOL_SPECS as _CUSTOM_AGENTS_TOOL_SPECS,
    )
except Exception:
    CUSTOM_AGENTS_AVAILABLE = False
    _CUSTOM_AGENTS_TOOL_FUNCTIONS, _CUSTOM_AGENTS_TOOL_SPECS = {}, []

if CUSTOM_AGENTS_AVAILABLE:
    TOOL_FUNCTIONS.update(_CUSTOM_AGENTS_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_CUSTOM_AGENTS_TOOL_SPECS)

# ---------------------------------------------------------------------------
# Plugins (list_plugins / install_plugin)
# ---------------------------------------------------------------------------
# A packaging/distribution layer over skills.py/custom_agents.py/
# mcp_client.py (already real, tested features), plus 2 small new
# capabilities: direct `/name` skill invocation (wired into main.py, not
# here) and a narrow, real subset of lifecycle hooks (SessionStart/
# PreToolUse/PostToolUse/Stop, wired into agent.py's own ReAct loop at 4
# REAL existing extension points -- not the full ~30-event Claude Code
# catalog, most of which has no analog anywhere in this project's actual
# architecture). See plugins.py's own module docstring for the full,
# fact-checked writeup (verified against code.claude.com/docs/en/plugins,
# plugins-reference, plugin-marketplaces -- official docs -- plus several
# independently-published current explainers, never Gitlawb/openclaude's
# leaked src/). plugins.py itself only imports `tools` LAZILY, matching
# every other optional module's established circular-import-avoidance
# pattern.
try:
    from plugins import PLUGINS_AVAILABLE, TOOL_FUNCTIONS as _PLUGINS_TOOL_FUNCTIONS, TOOL_SPECS as _PLUGINS_TOOL_SPECS
except Exception:
    PLUGINS_AVAILABLE = False
    _PLUGINS_TOOL_FUNCTIONS, _PLUGINS_TOOL_SPECS = {}, []

if PLUGINS_AVAILABLE:
    TOOL_FUNCTIONS.update(_PLUGINS_TOOL_FUNCTIONS)
    TOOL_SPECS.extend(_PLUGINS_TOOL_SPECS)
