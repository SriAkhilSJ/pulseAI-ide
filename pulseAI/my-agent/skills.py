"""
skills.py
---------
Agent Skills -- reusable, on-demand instruction bundles the model can load
mid-task via a "load_skill" tool, per Anthropic's OFFICIALLY PUBLISHED spec
(code.claude.com/docs/en/skills, agentskills.io) -- verified against the
real docs this session, not extracted from any leaked source (see
COMPARISON_openclaude.md for the standing legal/IP policy this project
follows for every Claude-Code-adjacent feature).

Format: a directory per skill under SKILLS_DIR, each containing a
SKILL.md with YAML frontmatter (--- delimited) + a markdown body:

    .agent_skills/
      react-component/
        SKILL.md
        templates/Component.tsx.template   (optional supporting file)

Three-layer progressive disclosure, per the official spec:
  1. METADATA (name + description) -- always in the system prompt, for
     every scanned skill, cheap enough for dozens of skills.
  2. BODY -- loaded only when the model calls load_skill(name).
  3. SUPPORTING FILES (templates/, references/, etc.) -- never auto-loaded;
     the skill body tells the model to read_file() them if actually needed.

DESIGN DECISIONS (fact-checked against Anthropic's real, official docs
during this session, not assumed):

1. allowed-tools/disallowed-tools are DELIBERATELY NOT ENFORCED in v1.
   Two real, still-open Anthropic GitHub issues (anthropics/claude-code
   #18837, #37683) show `allowed-tools` was widely MISUNDERSTOOD as a
   restriction and is documented (code.claude.com/docs/en/skills) to do
   the OPPOSITE of what those bug reports expected: it PRE-APPROVES tools
   to skip a confirmation prompt, and explicitly "does not restrict which
   tools are available." The real restriction field is `disallowed-tools`
   ("remove tools from Claude's available pool while a skill is active").
   Implementing disallowed_tools with REAL enforcement in this project
   would require making agent.run_agent's active_tool_functions/
   active_tool_specs -- currently computed ONCE before the ReAct loop
   starts and never touched again inside it -- into loop-mutable state
   updated mid-task when load_skill is dispatched. That's a real,
   non-trivial change to a loop this project has deliberately hardened
   carefully across several recent features (streaming, the batching
   nudge, sub-agent depth/budget). The cheaper, ALREADY-BUILT path to real
   enforcement when a task genuinely needs it: dispatch the skill as a
   sub-agent (subagents.py's dispatch_agent, which restricts tools at
   SPAWN time, no mid-loop mutation needed) -- e.g.
   dispatch_agent(subagent_type="explore", prompt=<skill body> + <task>).
   v1 here surfaces tools_hint purely as ADVISORY TEXT in load_skill's
   output (mirrors the real, if surprising, current behavior of
   allowed-tools in Anthropic's own product) -- escalate to real
   enforcement only if a future measurement shows advisory text alone
   isn't sufficient, per this project's established build-cheap-first,
   measure-then-escalate practice (see the batching-nudge decision).

2. A REAL BUG FOUND AND FIXED before this shipped: parsing a skill's
   frontmatter can raise (malformed YAML, or a SKILL.md missing its
   closing `---` delimiter entirely -- confirmed directly:
   `"---\\nname: x\\nno closing delimiter".split("---", 2)` returns only 2
   parts, and unpacking into 3 raises `ValueError` uncaught). A naive
   scan_skills() looping over every skill directory with NO per-skill
   exception handling means ONE malformed SKILL.md (a plausible typo in a
   hand-written file) would crash the WHOLE skills registry, silently
   taking down every OTHER valid skill too -- the same class of "one bad
   input kills everything" bug this project has already been bitten by and
   fixed before (see tools.py's null-args TypeError bug, and the
   rag_indexer/git_tools circular-import silent-failure bug). Fixed with a
   per-skill try/except that logs a clear warning and skips just that one
   skill, matching the project's established defensive philosophy.

3. `.agent_skills/` is dot-prefixed for naming CONSISTENCY with
   `.agent_backups/`/`.agent_missions/`, but is semantically DIFFERENT from
   both: those are pure runtime state (regenerated, gitignored); skills are
   USER-AUTHORED CONTENT meant to be committed and shared, more like
   test/finance_dashboard/ than a runtime cache. Not added to .gitignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import yaml
    SKILLS_AVAILABLE = True
except Exception:
    SKILLS_AVAILABLE = False

# NOTE: `tools` is imported LAZILY (inside _get_tools(), on first actual
# function call), exactly like git_tools.py/rag_indexer.py -- see their own
# module docstrings for the full circular-import rationale this avoids
# (tools.py imports skills.py at its own end to register load_skill/
# get_available_skills as agent tools; a module-level `import tools` here
# would risk the same silent partial-load failure those two modules
# document and fixed).
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


SKILLS_DIR_NAME = ".agent_skills"


def _skills_dir() -> Path:
    """Resolved lazily against tools.WORKDIR (not a module-level constant)
    so tests can point this at an isolated directory without needing
    tools.py to already be imported -- same lazy-resolution pattern as
    rag_indexer.py's _get_index_dir()."""
    return _get_tools().WORKDIR / SKILLS_DIR_NAME


@dataclass
class Skill:
    name: str
    description: str
    body: str
    root: Path
    tools_hint: Optional[str] = None  # advisory only -- see module docstring, decision 1


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Shared, reusable primitive: split `text` into (meta_dict, body) at its
    YAML frontmatter delimiters. Raises ValueError with a clear, specific
    message on any malformed input (missing/unclosed delimiter, malformed
    YAML, frontmatter that isn't a mapping) -- NEVER silently returns a
    partial/garbage result.

    Extracted out of parse_skill_text (this module's original, skill-
    specific caller) so rules.py can reuse the EXACT SAME parsing logic
    for its own frontmatter (paths:, description:) instead of
    reimplementing it -- per this project's own established practice of
    never maintaining two parallel implementations of the same parsing
    rule (see e.g. tools.is_sensitive_path being the single canonical
    check reused everywhere, instead of every module inventing its own).

    Uses a regex with DOTALL + non-greedy matching instead of the more
    obvious `text.split("---", 2)` -- confirmed directly that the naive
    split IS safe against a `---` appearing INSIDE the body (Python's
    str.split(sep, maxsplit) correctly stops after `maxsplit` splits), but
    is NOT safe against a missing/malformed closing delimiter (returns
    fewer than 3 parts, raising an unhandled ValueError on unpacking
    rather than a clear, catchable error). The regex fails predictably
    (returns None -> an explicit ValueError with a clear message) for that
    case instead.
    """
    if not SKILLS_AVAILABLE:
        raise RuntimeError("skills.py requires PyYAML, which is not installed.")

    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(
            "file must start with '---', contain YAML frontmatter, and "
            "have a closing '---' delimiter before the body."
        )
    frontmatter_text, body = match.group(1), match.group(2)

    try:
        meta = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise ValueError(f"malformed YAML frontmatter: {e}") from e

    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("YAML frontmatter must be a mapping (key: value pairs), not a list/scalar.")

    return meta, body.strip()


def parse_skill_text(text: str, root: Path) -> Skill:
    """
    Parse a SKILL.md's raw text into a Skill. Raises ValueError with a
    clear, specific message on any malformed input (missing/unclosed
    frontmatter delimiter, malformed YAML, missing required `name`/
    `description` fields) -- NEVER silently returns a partial/garbage
    Skill. Callers (scan_skills) are responsible for catching this per
    skill so one bad file doesn't take down the whole registry -- see
    module docstring, decision 2, for why that separation matters.

    Delegates the actual frontmatter/body split to parse_frontmatter
    (shared with rules.py -- see that function's own docstring).
    """
    meta, body = parse_frontmatter(text)

    name = meta.get("name")
    description = meta.get("description")
    if not name or not isinstance(name, str):
        raise ValueError("frontmatter must include a non-empty string 'name' field.")
    if not description or not isinstance(description, str):
        raise ValueError("frontmatter must include a non-empty string 'description' field.")

    tools_hint = meta.get("disallowed-tools") or meta.get("allowed-tools")
    if tools_hint is not None and not isinstance(tools_hint, str):
        # Accept a YAML list too (e.g. "disallowed-tools: [Bash, Write]"),
        # normalized to a comma-joined string for display -- advisory only,
        # never parsed back into a structured restriction (see decision 1).
        try:
            tools_hint = ", ".join(str(t) for t in tools_hint)
        except TypeError:
            tools_hint = str(tools_hint)

    return Skill(
        name=name.strip(),
        description=description.strip(),
        body=body,
        root=root,
        tools_hint=tools_hint,
    )


def scan_skills(skills_dir: Optional[Path] = None) -> dict[str, tuple[Optional[Skill], Optional[str]]]:
    """
    Scan `skills_dir` (defaults to the real .agent_skills/) for every
    subdirectory containing a SKILL.md, parsing each one INDEPENDENTLY --
    a malformed skill never prevents any other valid skill from loading
    (see module docstring, decision 2, for the real bug this fixes).

    Returns {skill_name_or_dirname: (Skill_or_None, error_or_None)} --
    both a Skill dict (for the happy path) and a coarse-grained
    directory-name-keyed error report (so a caller can surface "the
    'react-component' skill failed to load: <reason>" rather than that
    skill just silently not existing with no explanation at all).
    """
    directory = skills_dir if skills_dir is not None else _skills_dir()
    results: dict[str, tuple[Optional[Skill], Optional[str]]] = {}

    if not directory.exists() or not directory.is_dir():
        return results

    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            skill = parse_skill_text(text, root=entry)
        except Exception as e:
            # Per-skill isolation: THIS is the fix for the real bug found
            # while building this module -- one malformed SKILL.md must
            # never crash the whole scan or silently swallow every other
            # skill along with it.
            results[entry.name] = (None, f"{type(e).__name__}: {e}")
            continue
        results[skill.name] = (skill, None)

    return results


def get_metadata_block(skills: dict[str, tuple[Optional[Skill], Optional[str]]]) -> str:
    """Layer 1: cheap name+description text for the system prompt. Skills
    that failed to parse are NOT listed here (nothing usable to show), but
    ARE still visible via skill errors in list_skills()'s own tool output,
    so a broken skill doesn't just vanish without any trace."""
    valid = [skill for skill, err in skills.values() if skill is not None]
    if not valid:
        return ""
    lines = [
        "Available Skills (call load_skill(name) when a task matches a description below):"
    ]
    for skill in valid:
        lines.append(f"- {skill.name}: {skill.description}")
    return "\n".join(lines)


def render_loaded_skill(skill: Skill) -> str:
    """Layer 2: the full skill body + a pointer toward supporting files
    (Layer 3), returned as load_skill's tool result -- becomes a normal
    tool-result message the model sees on its next turn, exactly like any
    other tool's string output (no new message role/wire format)."""
    parts = [f"# Skill: {skill.name}", skill.body]
    if skill.tools_hint:
        parts.append(
            f"\n(This skill's frontmatter hints at tool usage: {skill.tools_hint} -- "
            "this is ADVISORY guidance from the skill author, not an enforced "
            "restriction; you should still follow it, but nothing here "
            "structurally prevents using other tools if genuinely needed.)"
        )
    try:
        supporting = sorted(
            p.relative_to(skill.root).as_posix()
            for p in skill.root.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
    except Exception:
        supporting = []
    if supporting:
        # skill.root is always an absolute path in real use (scan_skills
        # builds it from _skills_dir(), itself resolved from
        # tools.WORKDIR), but this is defensive against ANY root
        # (relative, or outside WORKDIR entirely, e.g. in a test) --
        # confirmed directly that Path.relative_to() raises ValueError
        # for a path that isn't actually a subpath of WORKDIR, which a
        # test constructing a Skill with a relative root can trigger.
        # Falls back to the skill's own directory name rather than
        # crashing what is otherwise a successful skill load.
        try:
            rel_root = skill.root.resolve().relative_to(_get_tools().WORKDIR.resolve()).as_posix()
        except ValueError:
            rel_root = skill.root.name
        parts.append(
            f"\n(Supporting files in this skill's directory ({rel_root}/) -- "
            f"use read_file if the instructions above reference one of these: "
            + ", ".join(supporting) + ")"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool wrappers -- registered into tools.TOOL_FUNCTIONS/TOOL_SPECS from
# tools.py's own "Optional skills" section, exactly like git_tools.py/
# rag_indexer.py/subagents.py's own tools are.
# ---------------------------------------------------------------------------

def _tool_load_skill(name: str) -> str:
    scanned = scan_skills()
    skill, err = scanned.get(name, (None, None))
    if skill is not None:
        return render_loaded_skill(skill)
    if err is not None:
        return f"ERROR: skill '{name}' exists but failed to load: {err}"
    available = sorted(s.name for s, e in scanned.values() if s is not None)
    return f"ERROR: no skill named '{name}' found. Available skills: {available or '(none)'}"


def _tool_list_skills() -> str:
    """A real, explicit tool (not just the always-on system-prompt
    metadata block) so the model -- or a human debugging why a skill isn't
    showing up -- can also see PARSE ERRORS for broken skills, which the
    system-prompt metadata block deliberately omits (see
    get_metadata_block's own docstring)."""
    scanned = scan_skills()
    if not scanned:
        return f"(no skills found in {_skills_dir()})"
    lines = []
    for key, (skill, err) in scanned.items():
        if skill is not None:
            lines.append(f"- {skill.name}: {skill.description}")
        else:
            lines.append(f"- {key}: ERROR (failed to load) -- {err}")
    return "\n".join(lines)


TOOL_FUNCTIONS = {
    "load_skill": _tool_load_skill,
    "list_skills": _tool_list_skills,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load a skill's full instructions into context by name -- use this when "
                "the current task matches one of the descriptions in the 'Available "
                "Skills' list in your system prompt. The skill's instructions will appear "
                "as this tool's result; follow them for the rest of this task. Supporting "
                "files the skill references (templates, examples) are NOT auto-loaded -- "
                "use read_file on them if the skill's instructions point you to one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact skill name from the Available Skills list."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": (
                "List every skill found, including ones that failed to parse (with the "
                "parse error) -- use this to debug why an expected skill isn't showing up "
                "in the Available Skills list, or to double-check exact skill names before "
                "calling load_skill."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
