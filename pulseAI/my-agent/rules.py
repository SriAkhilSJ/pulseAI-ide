"""
rules.py
--------
Custom project rules -- user-authored, version-controlled standing
instructions, following the real conventions verified against BOTH
Cursor (.cursor/rules/*.mdc, description/globs/alwaysApply frontmatter)
and Claude Code (CLAUDE.md + .claude/rules/*.md, paths: frontmatter for
path-scoped rules) -- see code.claude.com/docs/en/memory and multiple
current Cursor docs, verified this session, not extracted from any leaked
source.

Two kinds of rules, matching the real, verified behavior of both tools:

1. ALWAYS-LOADED rules -- a root file (AGENTS.md if present, since that's
   a genuinely open, cross-tool standard -- originated by OpenAI, donated
   to the Linux Foundation's Agentic AI Foundation, read natively by 20+
   tools including Claude Code as a fallback and Cursor -- verified this
   session, confirmed no naming collision with anything in this project)
   plus every rule file in .agent_rules/ that has NO `paths:` frontmatter
   (or no frontmatter at all -- a rule file can be plain text/markdown
   with zero YAML, exactly like a real .cursorrules or CLAUDE.md). These
   are injected into EVERY task's system prompt, computed fresh per
   run_agent() call for the exact same reason skills.py's metadata block
   is (see that module's own docstring): unlike a package-availability
   flag, these are user-authored FILES that can change on disk between
   calls within the same process.

2. PATH-SCOPED rules -- a rule file WITH `paths: <glob>` frontmatter is
   NOT loaded upfront. It's injected as a corrective observation the
   FIRST time (per task) a matching file is actually read or written --
   reusing the EXACT injection mechanism already proven live in agent.py's
   batching nudge (append to the tool-result CONTENT, never a new message
   role -- see that feature's own comments on why, re: the real Cerebras
   message-validation bug). This matches Claude Code's/Cursor's real,
   documented behavior (a path-scoped rule only "loads" when the agent
   actually touches a matching file), and is architecturally IDENTICAL to
   the batching nudge's own "detect a real-time condition mid-loop, inject
   into the next tool result" pattern -- built as a natural extension of
   proven code, not a new mechanism.

A REAL BUG, in Python's own stdlib, found and fixed before this shipped:
pathlib.PurePosixPath.match() does NOT implement real "**" (globstar)
semantics -- confirmed directly: `PurePosixPath("src/api/foo.ts").match(
"src/api/**/*.ts")` returns False, even though EVERY real doc example for
both Cursor and Claude Code uses exactly this pattern shape expecting it
to match a file directly inside src/api/, not just nested ones (glob
semantics: "**" matches ZERO OR MORE directories). Confirmed the correct
primitive instead: `glob.glob(pattern, recursive=True)` (which needs real
files on disk) and, for matching an arbitrary candidate STRING without
touching the filesystem, `glob.translate(pattern, recursive=True,
include_hidden=True)` (Python 3.13+, confirmed present in this
environment) -- translates a glob pattern into a real regex with correct
globstar semantics, verified directly against 8 test cases covering exact
matches, direct-child matches, nested matches, and non-matches, all
correct. A silent, wrong path-matching bug (e.g. a rule scoped to
"src/api/**/*.ts" silently never firing for files DIRECTLY in src/api/,
only nested ones) would have been far worse than an unhandled crash --
it fails "successfully" and nobody notices the rule never fires.

Rule file format (frontmatter optional -- a rule can be plain text/
markdown with zero YAML, exactly like a real .cursorrules/CLAUDE.md):

    .agent_rules/
      testing.md          <- no frontmatter -> always-loaded
      api-conventions.md  <- --- \n paths: src/api/**/*.ts \n --- \n ...

Reuses skills.py's parse_frontmatter() for the actual frontmatter/body
split (see that function's own docstring for why this project keeps ONE
canonical parsing implementation, not two) -- rules.py itself only adds
the `paths:` field and the "frontmatter is entirely optional" behavior
skills.py's SKILL.md format doesn't have (a SKILL.md always requires
name+description; a rule file requires neither).
"""

from __future__ import annotations

import glob as _glob_module
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import skills as _skills_module
    RULES_AVAILABLE = _skills_module.SKILLS_AVAILABLE  # same PyYAML dependency, same availability flag
except Exception:
    RULES_AVAILABLE = False
    _skills_module = None

# NOTE: `tools` is imported LAZILY (inside _get_tools(), on first actual
# function call), exactly matching git_tools.py/rag_indexer.py/skills.py's
# own established circular-import-avoidance pattern.
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


RULES_DIR_NAME = ".agent_rules"
ROOT_RULE_FILENAMES = ("AGENTS.md",)  # genuinely open, cross-tool standard -- see module docstring


def _rules_dir() -> Path:
    """Resolved lazily against tools.WORKDIR, same pattern as
    skills.py's own _skills_dir()."""
    return _get_tools().WORKDIR / RULES_DIR_NAME


@dataclass
class Rule:
    name: str          # derived from filename (no name: field required, unlike skills)
    body: str
    source_path: Path
    paths_glob: Optional[str] = None  # None = always-loaded; a glob string = path-scoped


def parse_rule_text(text: str, name: str, source_path: Path) -> Rule:
    """
    Parse a rule file's raw text into a Rule. Unlike skills.py's
    parse_skill_text, frontmatter here is ENTIRELY OPTIONAL -- a rule can
    be plain markdown/text with zero YAML (matching a real .cursorrules or
    CLAUDE.md, which have no required frontmatter at all), OR it can have
    a `paths:` field to scope it (matching Claude Code's real, documented
    `paths:` rule frontmatter and Cursor's `globs:` field).

    Raises ValueError only if frontmatter IS present but malformed (a
    real, deliberately unclosed/broken --- block) -- a file with no
    frontmatter delimiters at all is valid and treated as the whole file
    being the body, no error.
    """
    stripped = text.lstrip()
    if stripped.startswith("---"):
        # Reuses skills.py's exact frontmatter/body split -- see that
        # function's own docstring for why this project keeps one
        # canonical implementation.
        meta, body = _skills_module.parse_frontmatter(text)
        paths_glob = meta.get("paths") or meta.get("globs")
        if paths_glob is not None and not isinstance(paths_glob, str):
            # Accept a YAML list too (multiple glob patterns), joined with
            # '|' into a single regex alternation at match time -- see
            # rule_matches_path below.
            try:
                paths_glob = "|".join(str(p) for p in paths_glob)
            except TypeError:
                paths_glob = str(paths_glob)
    else:
        # No frontmatter at all -- the WHOLE file is the body, always-loaded.
        body = text.strip()
        paths_glob = None

    return Rule(name=name, body=body, source_path=source_path, paths_glob=paths_glob)


def _translate_glob_patterns(pattern_string: str) -> re.Pattern:
    """
    Compile `pattern_string` (one glob, or several joined with '|' from a
    YAML list -- see parse_rule_text) into a single regex matching ANY of
    them, using glob.translate for CORRECT globstar ("**") semantics.

    THE REAL BUG this avoids (see module docstring): pathlib.PurePosixPath
    .match() does NOT implement real globstar semantics -- confirmed
    directly that "src/api/**/*.ts" incorrectly fails to match
    "src/api/foo.ts" (a file DIRECTLY inside src/api/, not nested) via
    pathlib, while glob.translate's regex correctly matches it, matching
    real glob semantics ("**" matches zero or more directories) and every
    real Cursor/Claude Code doc example using this exact pattern shape.
    """
    sub_patterns = pattern_string.split("|")
    regexes = [
        _glob_module.translate(p.strip(), recursive=True, include_hidden=True)
        for p in sub_patterns if p.strip()
    ]
    combined = "|".join(f"(?:{r})" for r in regexes)
    return re.compile(combined)


def rule_matches_path(rule: Rule, relative_path: str) -> bool:
    """True if `rule` is path-scoped (has a paths_glob) AND `relative_path`
    (POSIX-style, relative to the project root -- e.g. "src/api/foo.ts")
    matches it. A rule with no paths_glob (always-loaded) never "matches"
    via this function -- it's already in every task's system prompt from
    the start, so there's nothing to trigger mid-task."""
    if not rule.paths_glob:
        return False
    try:
        pattern = _translate_glob_patterns(rule.paths_glob)
    except Exception:
        # A malformed glob pattern in a rule's frontmatter must not crash
        # the whole dispatch loop -- treat it as "never matches" and let
        # list_rules() surface the real problem for the user to fix,
        # exactly the same fail-safe philosophy as skills.py's per-skill
        # try/except in scan_skills().
        return False
    normalized = relative_path.replace("\\", "/")
    return bool(pattern.match(normalized))


def scan_rules(rules_dir: Optional[Path] = None) -> dict[str, tuple[Optional[Rule], Optional[str]]]:
    """
    Scan `rules_dir` (defaults to the real .agent_rules/) for every *.md
    file, parsing each INDEPENDENTLY -- one malformed rule file's
    frontmatter must never prevent any other valid rule from loading (the
    same real bug class skills.py's scan_skills() was built to avoid --
    see that module's own docstring for the original discovery).

    Returns {rule_name: (Rule_or_None, error_or_None)}, same shape as
    skills.scan_skills() for consistency.
    """
    directory = rules_dir if rules_dir is not None else _rules_dir()
    results: dict[str, tuple[Optional[Rule], Optional[str]]] = {}

    if not directory.exists() or not directory.is_dir():
        return results

    for entry in sorted(directory.rglob("*.md")):
        if not entry.is_file():
            continue
        name = entry.stem
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
            rule = parse_rule_text(text, name=name, source_path=entry)
        except Exception as e:
            results[name] = (None, f"{type(e).__name__}: {e}")
            continue
        results[name] = (rule, None)

    return results


def _load_root_rule_file() -> Optional[str]:
    """Load AGENTS.md from the project root if present -- see module
    docstring for why this is the one root filename supported (a real,
    genuinely open cross-tool standard, not a leaked-source-adjacent
    convention). Returns None if absent; never raises (a root rule file is
    plain text/markdown, nothing to parse that could be malformed)."""
    for filename in ROOT_RULE_FILENAMES:
        path = _get_tools().WORKDIR / filename
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                return None
    return None


def get_always_loaded_block(rules: dict[str, tuple[Optional[Rule], Optional[str]]]) -> str:
    """
    Builds the text injected into EVERY task's system prompt: the root
    AGENTS.md (if present) followed by every rule file that has NO
    paths_glob (always-loaded, exactly like a real .cursorrules or a
    CLAUDE.md rule file with no `paths:` frontmatter).

    Path-scoped rules are DELIBERATELY EXCLUDED here -- they're injected
    later, mid-task, only when a matching file is actually touched (see
    module docstring, and agent.py's wiring of this alongside the
    batching nudge).
    """
    parts = []
    root_content = _load_root_rule_file()
    if root_content:
        parts.append(f"# Project Rules (AGENTS.md)\n{root_content}")

    always_loaded = [
        rule for rule, err in rules.values()
        if rule is not None and not rule.paths_glob
    ]
    for rule in always_loaded:
        parts.append(f"# Project Rule: {rule.name}\n{rule.body}")

    return "\n\n".join(parts)


def get_path_scoped_rules(rules: dict[str, tuple[Optional[Rule], Optional[str]]]) -> list[Rule]:
    """Every rule that HAS a paths_glob -- these are checked against
    file-touching tool calls mid-task (see agent.py's wiring), never
    injected upfront."""
    return [rule for rule, err in rules.values() if rule is not None and rule.paths_glob]


# ---------------------------------------------------------------------------
# Tool wrapper -- a debugging/introspection tool (like skills.py's
# list_skills), NOT a "load_rule" tool -- rules are never explicitly
# loaded by the model the way skills are; they're either always-on or
# triggered automatically by file access, matching real Cursor/Claude Code
# behavior where rules aren't something the AI chooses to invoke.
# ---------------------------------------------------------------------------

def _tool_list_rules() -> str:
    """Lists every discovered rule (always-loaded or path-scoped, plus any
    that failed to parse with their real error) -- so the model (or a
    human debugging why a rule isn't firing) can see the full picture,
    mirroring skills.py's list_skills tool."""
    rules = scan_rules()
    root_content = _load_root_rule_file()
    lines = []
    if root_content:
        lines.append(f"- AGENTS.md (root, always-loaded, {len(root_content)} chars)")
    if not rules and not root_content:
        return f"(no rules found -- no AGENTS.md at project root, and {_rules_dir()} has no *.md files)"
    for name, (rule, err) in rules.items():
        if rule is not None:
            scope = f"path-scoped: {rule.paths_glob}" if rule.paths_glob else "always-loaded"
            lines.append(f"- {name} ({scope})")
        else:
            lines.append(f"- {name}: ERROR (failed to load) -- {err}")
    return "\n".join(lines)


TOOL_FUNCTIONS = {
    "list_rules": _tool_list_rules,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_rules",
            "description": (
                "List every custom project rule found (AGENTS.md at the project root, plus "
                "files in .agent_rules/), showing whether each is always-loaded or scoped to "
                "specific file paths, and surfacing parse errors for any rule that failed to "
                "load. Use this to debug why an expected rule doesn't seem to be in effect."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
