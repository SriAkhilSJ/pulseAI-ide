"""
git_tools.py
------------
Semantic git operations for the agent, replacing raw `run_command("git ...")`
calls with structured, safety-checked ones -- so committing/branching goes
through the SAME secret-detection logic as every other tool
(tools.is_sensitive_path), instead of a separate, narrower check that can
have its own gaps.

Real bug found and fixed while designing this (NOT shipped): a first draft
followed a proposed design where the "don't commit secrets" check only
scanned `repo.untracked_files` for filenames containing ".env"/"secret".
Reproduced directly against a throwaway repo that this misses two real
cases:
  1. A file that's already TRACKED (e.g. .env got committed by mistake
     earlier) and is merely MODIFIED now -- `untracked_files` doesn't
     include it at all, so a substring-only untracked-file check would
     silently let a secret change be committed.
  2. Untracked files don't show up in `repo.index.diff(None)` (or `git diff`)
     at all -- confirmed empty diff for a real untracked file -- so a
     diff-preview built only from `index.diff` silently omits new secret
     files from what the user/agent sees before committing.

Fixed by:
  - Building the changed-files list from the UNION of `repo.untracked_files`
    and `repo.index.diff(None)` (modified/staged-vs-working) entries, so
    both new and modified files are visible.
  - Checking EVERY path in that union against tools.is_sensitive_path()
    (the same, already-tested guardrail used by read_file/write_file/
    run_command/grep_files) before allowing add/commit -- not a separate,
    narrower keyword list that can drift out of sync with the real one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    import git
    from git import Repo
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

# NOTE: `tools` is imported LAZILY (inside functions, via _get_tools() below)
# rather than at module level. Real bug found and fixed: tools.py imports
# git_tools at its own end (to register git_* as agent tools), while this
# module needs tools.WORKDIR/tools.is_sensitive_path. A module-level
# `import tools as _tools` here creates a genuine circular import --
# whichever of the two modules is imported FIRST works fine, but if
# something imports git_tools.py (or rag_indexer.py, same issue) BEFORE
# ever importing tools.py, Python has to load tools.py mid-way through
# loading git_tools.py, tools.py's own `from git_tools import ...` then
# hits a partially-initialized git_tools module that doesn't have
# TOOL_FUNCTIONS/TOOL_SPECS defined yet -- an ImportError that tools.py's
# broad `except Exception` swallows SILENTLY, permanently disabling
# GIT_AVAILABLE for the rest of that process with no visible error.
# Reproduced directly: `import git_tools` (standalone, before tools) then
# `import tools` afterward showed `tools.GIT_AVAILABLE == False` even
# though git_tools imported fine on its own. Deferring the `tools` import
# to first actual use avoids the cycle entirely -- by the time any of
# these functions actually RUNS, both modules have finished loading.
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


class GitError(Exception):
    """Raised for git operations that can't proceed safely (not a repo,
    sensitive file detected, dirty working tree when it shouldn't be, etc.)."""
    pass


def _open_repo(repo_path: Optional[str] = None) -> "Repo":
    path = repo_path or str(_get_tools().WORKDIR)
    try:
        return Repo(path)
    except git.NoSuchPathError:
        raise GitError(f"No such path: {path}")
    except git.InvalidGitRepositoryError:
        raise GitError(
            f"{path} is not a git repository. Run git_init first if you "
            "actually want one here -- this is not done automatically."
        )


def git_init(repo_path: Optional[str] = None) -> str:
    """Initialize a new git repository. Deliberately a separate, explicit
    tool (not run automatically by any other git_* function) so an agent
    can never silently turn a plain directory into a git repo as a side
    effect of some other request."""
    path = repo_path or str(_get_tools().WORKDIR)
    p = Path(path)
    if (p / ".git").exists():
        return f"'{path}' is already a git repository."
    try:
        Repo.init(path)
        return f"Initialized a new git repository at {path}."
    except Exception as e:
        return f"ERROR: could not initialize git repo at {path}: {e}"


def _changed_paths(repo: "Repo") -> list[str]:
    """Every path that differs from HEAD/index in some way: untracked
    (brand new) files AND modified/staged tracked files. See module
    docstring for why BOTH sources are required -- neither alone is a
    complete picture of what a commit would actually include."""
    paths = set(repo.untracked_files)
    for diff_item in repo.index.diff(None):
        if diff_item.a_path:
            paths.add(diff_item.a_path)
        if diff_item.b_path:
            paths.add(diff_item.b_path)
    # Also include staged-but-not-yet-committed changes (index vs HEAD),
    # relevant if something was `git add`-ed in an earlier turn but not
    # committed yet -- still shows up in a future commit.
    try:
        for diff_item in repo.index.diff("HEAD"):
            if diff_item.a_path:
                paths.add(diff_item.a_path)
            if diff_item.b_path:
                paths.add(diff_item.b_path)
    except Exception:
        # No HEAD yet (brand new repo, no commits) -- nothing to diff against.
        pass
    return sorted(paths)


def _sensitive_paths_in(paths: list[str]) -> list[str]:
    return [p for p in paths if _get_tools().is_sensitive_path(p)]


def git_status(repo_path: Optional[str] = None) -> str:
    """Structured status: branch, changed files (both tracked-modified and
    untracked), and whether any of them are sensitive paths that must never
    be committed."""
    repo = _open_repo(repo_path)
    try:
        branch = repo.active_branch.name
    except TypeError:
        branch = "(detached HEAD)"

    changed = _changed_paths(repo)
    sensitive = _sensitive_paths_in(changed)

    lines = [f"Branch: {branch}", f"Changed files ({len(changed)}):"]
    if not changed:
        lines.append("  (working tree clean)")
    for p in changed:
        flag = "  [SENSITIVE -- will be refused if you try to commit this]" if p in sensitive else ""
        lines.append(f"  {p}{flag}")
    if sensitive:
        lines.append(
            f"\nWARNING: {len(sensitive)} sensitive path(s) detected in the changes above. "
            "git_commit will refuse to include them."
        )
    return "\n".join(lines)


def git_diff(path: Optional[str] = None, repo_path: Optional[str] = None) -> str:
    """Unified diff text for uncommitted changes -- one specific path, or
    everything if path is omitted. Refuses to show diffs for sensitive
    paths (same guardrail as read_file: the CONTENT of a secret should
    never enter the LLM's context, and a diff is exactly that)."""
    repo = _open_repo(repo_path)

    if path is not None and _get_tools().is_sensitive_path(path):
        return f"ERROR: refusing to diff a sensitive path: {path}"

    changed = _changed_paths(repo)
    sensitive = set(_sensitive_paths_in(changed if path is None else [path]))

    # Real bug found and fixed by this module's own test suite: an earlier
    # version ran `repo.git.diff(*args)` UNFILTERED and only appended an
    # "excluded" NOTE alongside it -- the actual diff TEXT (including full
    # secret content, e.g. ".env"'s real before/after values) was still
    # returned in full underneath that note. A note next to a leak is still
    # a leak. Fixed by explicitly excluding every sensitive path from the
    # `git diff` invocation itself (via `-- <non-sensitive paths>` pathspec)
    # instead of trying to filter/redact the diff text after the fact.
    non_sensitive_changed = [p for p in changed if p not in sensitive]
    if path is not None:
        if path in sensitive:
            diff_targets = []  # already returned an error above, but stay defensive
        else:
            diff_targets = [path]
    else:
        diff_targets = non_sensitive_changed

    if sensitive:
        note = (
            f"\n[{len(sensitive)} sensitive path(s) excluded from this diff entirely "
            f"(not shown, not even redacted): {', '.join(sorted(sensitive))}]"
        )
    else:
        note = ""

    if diff_targets:
        tracked_diff = repo.git.diff("--no-color", "--", *diff_targets)
    else:
        tracked_diff = ""

    # git diff shows nothing for brand-new untracked files (confirmed
    # directly) -- surface them separately as "new file" entries so the
    # diff view isn't silently incomplete for the exact case this module
    # was built to catch. Sensitive untracked files are named in `note`
    # above but their CONTENT is never touched here.
    untracked_note = ""
    untracked_to_show = [
        p for p in repo.untracked_files
        if p not in sensitive and (path is None or p == path)
    ]
    if untracked_to_show:
        untracked_note = "\n\nUntracked (new) files not shown by `git diff` itself:\n" + "\n".join(
            f"  + {p}" for p in untracked_to_show
        )

    result = tracked_diff or "(no non-sensitive tracked-file changes)"
    return f"{result}{untracked_note}{note}"


def git_commit(message: str, include_untracked: bool = True, repo_path: Optional[str] = None) -> str:
    """Stage and commit changes, after refusing outright if any changed
    path (tracked-modified OR untracked -- see _changed_paths) looks
    sensitive. There is no override flag for this -- same policy as
    write_file/read_file: a confirmed leak isn't a safe leak."""
    repo = _open_repo(repo_path)

    changed = _changed_paths(repo)
    if not changed:
        return "Nothing to commit -- working tree is clean."

    sensitive = _sensitive_paths_in(changed)
    if sensitive:
        return (
            f"ERROR: refusing to commit -- {len(sensitive)} sensitive path(s) "
            f"detected among the changes: {', '.join(sensitive)}. "
            "Remove/unstage them (or add to .gitignore) before committing. "
            "This check cannot be overridden."
        )

    if include_untracked:
        repo.git.add(A=True)
    else:
        # Stage only tracked, already-known files (modifications), not new
        # untracked ones -- for a caller that wants an explicit, narrower
        # commit.
        repo.git.add(update=True)

    diff_summary = repo.git.diff("--stat", "--cached") or "(no staged changes to summarize)"

    try:
        commit = repo.index.commit(message)
    except Exception as e:
        return f"ERROR: commit failed: {e}"

    return f"Committed {commit.hexsha[:8]}: {message}\nFiles changed:\n{diff_summary}"


def git_log(max_count: int = 10, repo_path: Optional[str] = None) -> str:
    """Recent commit history, newest first."""
    repo = _open_repo(repo_path)
    try:
        commits = list(repo.iter_commits(max_count=max_count))
    except Exception as e:
        return f"(no commits yet, or error reading history: {e})"
    if not commits:
        return "(no commits yet)"
    lines = []
    for c in commits:
        msg = c.message.strip().splitlines()[0] if c.message else "(no message)"
        lines.append(f"{c.hexsha[:8]}  {msg}")
    return "\n".join(lines)


def git_create_branch(branch_name: str, repo_path: Optional[str] = None) -> str:
    """Create and switch to a new branch. Refuses if the working tree is
    dirty (uncommitted changes would silently follow onto the new branch,
    which is usually not what's intended -- ask the caller to commit or
    explicitly proceed with a plain run_command git stash if that's really
    what they want)."""
    repo = _open_repo(repo_path)
    if repo.is_dirty(untracked_files=True):
        return (
            "ERROR: working tree has uncommitted changes (including untracked "
            "files). Commit them first with git_commit, or they will not be "
            "cleanly separated between branches."
        )
    try:
        new_branch = repo.create_head(branch_name)
        new_branch.checkout()
        return f"Created and switched to new branch: {branch_name}"
    except Exception as e:
        return f"ERROR creating branch {branch_name}: {e}"


# ---------------------------------------------------------------------------
# Agent-callable tool wrappers + specs (registered into tools.py's
# TOOL_FUNCTIONS/TOOL_SPECS only if GIT_AVAILABLE -- see tools.py's
# "Optional git tools" section for why, and note git_init is deliberately
# a SEPARATE explicit tool the model must choose to call, never invoked
# automatically as a side effect of any other git_* call).
# ---------------------------------------------------------------------------

def _tool_git_init() -> str:
    return git_init()


def _tool_git_status() -> str:
    try:
        return git_status()
    except GitError as e:
        return f"ERROR: {e}"


def _tool_git_diff(path: str = "") -> str:
    try:
        return git_diff(path or None)
    except GitError as e:
        return f"ERROR: {e}"


def _tool_git_commit(message: str, include_untracked: bool = True) -> str:
    try:
        return git_commit(message, include_untracked=include_untracked)
    except GitError as e:
        return f"ERROR: {e}"


def _tool_git_log(max_count: int = 10) -> str:
    try:
        return git_log(max_count)
    except GitError as e:
        return f"ERROR: {e}"


def _tool_git_create_branch(branch_name: str) -> str:
    try:
        return git_create_branch(branch_name)
    except GitError as e:
        return f"ERROR: {e}"


TOOL_FUNCTIONS = {
    "git_init": _tool_git_init,
    "git_status": _tool_git_status,
    "git_diff": _tool_git_diff,
    "git_commit": _tool_git_commit,
    "git_log": _tool_git_log,
    "git_create_branch": _tool_git_create_branch,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "git_init",
            "description": (
                "Initialize a new git repository in the project root. This is NEVER "
                "done automatically by any other git_* tool -- you must call this "
                "explicitly if the project isn't already a git repo (git_status will "
                "say so clearly if it isn't)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Show the current branch and every changed file (both modified-tracked "
                "and untracked/new), flagging any that look like secrets (.env, keys, "
                "credentials) that git_commit will refuse to include."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Show a unified diff of uncommitted changes (all files, or one specific "
                "path). Sensitive paths (.env, keys, credentials) are NEVER shown here "
                "-- their content is excluded entirely, not just noted -- only their "
                "path is mentioned so you know they exist and are being withheld."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional: limit the diff to this one file path. Omit for the full diff.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "Stage and commit all current changes with the given message. Refuses "
                "outright (with no override) if ANY changed file -- tracked-modified or "
                "brand-new untracked -- looks like a secret (.env, private keys, "
                "credentials, etc). Fix/remove/gitignore the sensitive file(s) first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The commit message."},
                    "include_untracked": {
                        "type": "boolean",
                        "description": "Stage new (untracked) files too, not just modifications to already-tracked ones. Default true.",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent commit history (hash + first line of message), newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {"type": "integer", "description": "How many commits to show (default 10)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_create_branch",
            "description": (
                "Create and switch to a new branch. Refuses if the working tree has "
                "uncommitted changes (commit or discard them first) so changes don't "
                "silently follow onto the new branch unintentionally."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string", "description": "Name of the new branch."},
                },
                "required": ["branch_name"],
            },
        },
    },
]

