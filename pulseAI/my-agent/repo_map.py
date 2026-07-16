"""
repo_map.py
-----------
A PageRank-ranked map of the codebase's most structurally important
files (definitions + import graph), following the algorithm made popular
by Aider (github.com/Aider-AI/aider/blob/main/aider/repomap.py,
Apache-2.0, independently-open, safe to build from per this project's
standing leak-avoidance policy -- see COMPARISON_openclaude.md) and
described (without implementation) in OpenClaude's own public
docs/repo-map.md. Built clean-room against THIS project's actually-
installed libraries, not copied from either source.

WHY THIS EXISTS: helps the agent (and a human) quickly see which files
matter most in a codebase before diving into read_file calls one at a
time -- "app.py imports auth.py which imports models.py, and models.py
is imported by 5 other files" is exactly the kind of structural fact
that's expensive to reconstruct via grep/read_file but cheap to compute
once and cache.

FACT-CHECKED AGAINST THE ACTUALLY-INSTALLED tree-sitter 0.26.0 /
tree-sitter-python 0.25.0 / tree-sitter-javascript 0.25.0 before writing
any of this, because a proposed design had THREE real, confirmed bugs:

1. The proposal's own tree-sitter verification snippet
   (`parser.set_language(tspython.language())`) FAILS immediately --
   confirmed directly: `Parser` objects have no `set_language` method in
   this version (`AttributeError: 'tree_sitter.Parser' object has no
   attribute 'set_language'`). The real, already-proven pattern (see
   ast_tools.py, which fact-checked this exact same API months earlier
   this project) is `Parser(Language(grammar.language()))` -- language
   passed directly to the constructor. Also confirmed (matching
   ast_tools.py's own prior finding) that `Query` objects have no
   `.captures` attribute the proposal's snippet assumed
   (`hasattr(Query(...), "captures") == False`) -- the real API is
   `QueryCursor(query).matches(node)`, reusing ast_tools.py's own
   `_matches()` helper pattern rather than re-deriving it.

2. A REAL, SERIOUS bug in the proposed JS `require()` detection query:
   `(call_expression function: (identifier) @require)` matches EVERY
   function call in a file, not just `require(...)` calls -- confirmed
   directly against real code (`console.log(...)`, `calculateTotal(1,2)`,
   `someOtherFunction()` all got captured as "require" alongside the one
   real `require("./helpers")` call). Using this as-is would have
   corrupted the import graph with dozens of false dependency edges per
   file. Fixed with a `(#eq? @fn "require")` predicate -- the same exact
   predicate pattern already proven working in ast_tools.py's own
   `add_jsdoc_to_function` (`#eq? @name "..."` for exact function-name
   matching) -- confirmed live this only matches the real `require()`
   call, not other function calls.

3. `networkx` was assumed "already installed via some dependency" --
   confirmed this is FALSE as stated: it's present in this sandbox only
   by coincidence, pulled in transitively by `scikit-image` (unrelated to
   this project, not itself a real dependency of anything in
   requirements.txt). Unlike PyYAML (genuinely pulled in by chromadb, an
   ACTUAL project dependency, verified separately when building
   skills.py), relying on networkx here would be relying on sandbox
   coincidence, not a real guarantee. Per explicit decision this session:
   built a small, hand-rolled PageRank instead (see _pagerank below) --
   VERIFIED NUMERICALLY correct by comparing its output against
   networkx's real nx.pagerank() output on two test graphs (a normal
   multi-file import graph, and one containing an isolated node with zero
   edges) -- matched to 6 decimal places in both cases, including
   correctly handling the isolated-node case (edges alone can't reveal a
   file with zero imports/importers; the full node set must be passed in
   explicitly, a real gap found and fixed during that verification). See
   REPO_MAP_NETWORKX_UPGRADE.md for the documented upgrade path if a
   future, much larger codebase ever needs networkx's more optimized
   implementation (sparse matrix operations, faster convergence) --
   deliberately not built now per the same build-cheap-first,
   measure-then-escalate practice already used for the batching nudge and
   skills' tools_hint decision.

File enumeration: `git ls-files --cached --others --exclude-standard`
(matches OpenClaude's own documented approach, confirmed working via
direct subprocess call against this project's real repo -- correctly
excludes node_modules/ and other gitignored paths without needing a
separate exclusion list) with a fallback to a manual `Path.rglob()` walk
for a directory that isn't a git repo at all (confirmed `git ls-files`
exits non-zero outside a repo, handled explicitly rather than letting a
CalledProcessError propagate).

Caching: keyed by (path, mtime, size) per file, matching OpenClaude's own
documented cache-invalidation approach -- avoids re-parsing every file on
every call, following this project's own repeated pattern (RAG's
ChromaDB index, LSP's diagnostics) of not redoing expensive work that
hasn't changed.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_python as _tspy
    import tree_sitter_javascript as _tsjs
    REPO_MAP_AVAILABLE = True
except Exception:
    REPO_MAP_AVAILABLE = False

# NOTE: `tools` is imported LAZILY, matching every other optional
# module's established circular-import-avoidance pattern (see
# git_tools.py/rag_indexer.py/skills.py/rules.py's own docstrings for the
# original discovery of why this matters).
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


CACHE_DIR_NAME = ".agent_repomap_cache"
CACHE_FILE_NAME = "cache.json"
DEFAULT_TOKEN_BUDGET = 1500
MAX_SYMBOLS_PER_FILE_SHOWN = 8

_PY_LANGUAGE = None
_JS_LANGUAGE = None


def _get_py_language():
    global _PY_LANGUAGE
    if _PY_LANGUAGE is None:
        _PY_LANGUAGE = Language(_tspy.language())
    return _PY_LANGUAGE


def _get_js_language():
    global _JS_LANGUAGE
    if _JS_LANGUAGE is None:
        _JS_LANGUAGE = Language(_tsjs.language())
    return _JS_LANGUAGE


def _matches(language, pattern: str, node) -> list:
    """Run a tree-sitter query against `node`, returning the real
    (match_index, {capture_name: [nodes]}) shape -- reuses the exact
    verified pattern from ast_tools.py's own _matches() helper (see this
    module's docstring point 1 for the fact-check this is based on),
    parameterized by language since this module needs both Python and
    JS grammars, unlike ast_tools.py which only ever needed JS."""
    query = Query(language, pattern)
    cursor = QueryCursor(query)
    return cursor.matches(node)


@dataclass
class FileSymbols:
    path: str                          # relative to project root, POSIX-style
    definitions: list[str] = field(default_factory=list)   # function/class names defined here
    imports: list[str] = field(default_factory=list)       # module names/paths this file imports


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------

_PY_DEF_QUERY = """
(function_definition name: (identifier) @func)
(class_definition name: (identifier) @class)
"""
# Verified field names directly against the real parse tree (see module
# docstring): `name:` for the plain import target, `module_name:` for the
# source module in a `from X import Y` statement -- these are genuinely
# DIFFERENT fields, confirmed by walking the real tree with
# node.field_name_for_child(), not guessed.
#
# A real bug found by this module's OWN test suite (not caught during the
# initial fact-check): `import utils as u` wraps the dotted_name inside an
# `aliased_import` node, so `name:` on `import_statement` is the
# `aliased_import` node itself, not the `dotted_name` directly -- the
# first version of this query silently missed every aliased import.
# Confirmed by walking the real parse tree of `import utils as u`
# directly: `import_statement -> name: aliased_import -> name: dotted_name
# ("utils"), alias: identifier ("u")`. Fixed with a second query line that
# reaches into the aliased_import wrapper specifically.
# A second real bug found by this module's own test suite: `from . import
# helpers` and `from ..pkg import thing` wrap the source in a
# relative_import node (module_name: relative_import), not a plain
# dotted_name -- the original query only matched
# `module_name: (dotted_name)`, silently missing every relative import.
# Confirmed by walking the real parse tree: for a relative import, the
# meaningful RESOLVABLE target is the `name:` field (the actual imported
# symbol, e.g. "helpers"), not the module_name (just "." or ".."), since
# a bare relative-import prefix isn't itself a real file to resolve to --
# this is fixed by matching import_from_statement's `name:` field
# specifically WHEN module_name is a relative_import, which correctly
# does NOT also match `from auth import login, logout`'s `name:` fields
# (those are genuinely just symbol names, not files, and `auth` itself is
# still separately captured via the plain dotted_name case above).
_PY_IMPORT_QUERY = """
(import_statement name: (dotted_name) @import)
(import_statement name: (aliased_import name: (dotted_name) @import))
(import_from_statement module_name: (dotted_name) @import)
(import_from_statement module_name: (relative_import) name: (dotted_name) @import)
"""


def extract_python_symbols(source: str) -> tuple[list[str], list[str]]:
    """Returns (definitions, imports) for Python source text. Never
    raises on a syntax error -- tree-sitter parses incrementally/
    error-tolerantly by design, so a partially-invalid file still yields
    whatever definitions/imports it can find rather than an exception."""
    lang = _get_py_language()
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8", errors="replace"))
    root = tree.root_node

    definitions = []
    for _idx, captures in _matches(lang, _PY_DEF_QUERY, root):
        for key in ("func", "class"):
            for node in captures.get(key, []):
                definitions.append(node.text.decode("utf-8", errors="replace"))

    imports = []
    for _idx, captures in _matches(lang, _PY_IMPORT_QUERY, root):
        for node in captures.get("import", []):
            imports.append(node.text.decode("utf-8", errors="replace"))

    return definitions, imports


# ---------------------------------------------------------------------------
# JavaScript extraction
# ---------------------------------------------------------------------------

_JS_DEF_QUERY = """
(function_declaration name: (identifier) @func)
(class_declaration name: (identifier) @class)
"""
_JS_IMPORT_QUERY = """
(import_statement source: (string) @import)
"""
# THE fixed query (see module docstring point 2): the #eq? predicate is
# REQUIRED -- without it, this matches every function call in the file,
# not just require(). Verified directly this only matches literal
# `require(...)` calls, confirmed against real code containing
# console.log/other function calls that must NOT be captured.
_JS_REQUIRE_QUERY = """
(call_expression
  function: (identifier) @fn (#eq? @fn "require")
  arguments: (arguments (string) @import))
"""


def extract_javascript_symbols(source: str) -> tuple[list[str], list[str]]:
    """Returns (definitions, imports) for JavaScript source text, covering
    both ES module `import ... from "..."` and CommonJS `require("...")`
    (the fixed, predicate-filtered version -- see module docstring)."""
    lang = _get_js_language()
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8", errors="replace"))
    root = tree.root_node

    definitions = []
    for _idx, captures in _matches(lang, _JS_DEF_QUERY, root):
        for key in ("func", "class"):
            for node in captures.get(key, []):
                definitions.append(node.text.decode("utf-8", errors="replace"))

    imports = []
    for _idx, captures in _matches(lang, _JS_IMPORT_QUERY, root):
        for node in captures.get("import", []):
            # Strip the surrounding quotes -- the captured node's text is
            # the literal string INCLUDING quote characters (e.g. '"./auth"').
            text = node.text.decode("utf-8", errors="replace")
            imports.append(text.strip("'\"") )
    for _idx, captures in _matches(lang, _JS_REQUIRE_QUERY, root):
        for node in captures.get("import", []):
            text = node.text.decode("utf-8", errors="replace")
            imports.append(text.strip("'\""))

    return definitions, imports


_EXTRACTORS = {
    ".py": extract_python_symbols,
    ".js": extract_javascript_symbols,
    ".jsx": extract_javascript_symbols,
}


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------

# Common noise directories to defensively exclude EVEN when git's own
# --exclude-standard already handled it via .gitignore -- belt-and-
# suspenders, matching this project's own established pattern
# (tools.grep_files applies the same "exclude via flag, then defensively
# re-filter" approach for sensitive paths, not trusting the flag alone).
# A real, live gap found by this module's own test suite: a directory
# with NO .gitignore rule for node_modules at all (confirmed directly:
# this project's own ROOT .gitignore doesn't mention node_modules --
# node_modules exclusion for test/furniture_site/ comes from vite's own
# NESTED .gitignore inside that subdirectory specifically) would have
# `git ls-files --exclude-standard` return node_modules contents
# completely unfiltered for a fresh directory with no such rule.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}


def list_project_files(root: Optional[Path] = None) -> list[str]:
    """
    Returns relative (POSIX-style) paths of every file git considers part
    of the project (tracked + untracked-but-not-gitignored), matching
    OpenClaude's own documented approach, PLUS a defensive re-filter
    against _SKIP_DIRS regardless of whether a .gitignore rule already
    excluded them (see _SKIP_DIRS's own comment for the real gap this
    guards against). Falls back to a plain directory walk if `root` isn't
    a git repo at all (confirmed `git ls-files` exits non-zero outside a
    repo -- handled explicitly here rather than letting a
    CalledProcessError propagate to the caller).
    """
    workdir = root if root is not None else _get_tools().WORKDIR
    paths: list[str] = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(workdir), capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            paths = [line for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    if not paths:
        # Fallback: not a git repo (or git unavailable, or genuinely empty)
        # -- manual walk. _SKIP_DIRS filtering below still applies.
        for path in workdir.rglob("*"):
            if not path.is_file():
                continue
            paths.append(path.relative_to(workdir).as_posix())

    return [p for p in paths if not any(part in _SKIP_DIRS for part in Path(p).parts)]


# ---------------------------------------------------------------------------
# Caching -- keyed by (path, mtime, size), matching OpenClaude's own
# documented cache-invalidation approach.
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    return _get_tools().WORKDIR / CACHE_DIR_NAME / CACHE_FILE_NAME


def _load_cache() -> dict:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # cache is a pure optimization -- a write failure must never break the caller


def _file_cache_key(full_path: Path) -> Optional[str]:
    """A (mtime, size) fingerprint for a file -- cheap to compute, and
    catches both "file was edited" and "file was replaced with different
    content at the same mtime" (size differs) without hashing the whole
    file's content on every single check."""
    try:
        stat = full_path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def scan_repo(root: Optional[Path] = None, use_cache: bool = True) -> dict[str, FileSymbols]:
    """
    Parse every supported file (.py/.js/.jsx) in the project, extracting
    definitions and imports. Uses the on-disk cache (keyed by path+mtime+
    size) to skip re-parsing unchanged files -- confirmed this project's
    own real files (71 .py/.js files as of this writing) parse in well
    under a second cold, so the cache mainly matters for larger codebases
    this project doesn't have yet, but the mechanism is verified correct
    now rather than added later.
    """
    workdir = root if root is not None else _get_tools().WORKDIR
    cache = _load_cache() if use_cache else {}
    new_cache: dict = {}
    results: dict[str, FileSymbols] = {}

    for rel_path in list_project_files(workdir):
        suffix = Path(rel_path).suffix
        extractor = _EXTRACTORS.get(suffix)
        if extractor is None:
            continue

        full_path = workdir / rel_path
        key = _file_cache_key(full_path)
        if key is None:
            continue

        cache_entry = cache.get(rel_path)
        if cache_entry and cache_entry.get("key") == key:
            definitions = cache_entry["definitions"]
            imports = cache_entry["imports"]
        else:
            try:
                source = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                definitions, imports = extractor(source)
            except Exception:
                # A single unparseable file must never crash the whole
                # scan -- same defensive philosophy as skills.py's
                # per-skill try/except in scan_skills().
                definitions, imports = [], []

        new_cache[rel_path] = {"key": key, "definitions": definitions, "imports": imports}
        results[rel_path] = FileSymbols(path=rel_path, definitions=definitions, imports=imports)

    if use_cache:
        _save_cache(new_cache)

    return results


# ---------------------------------------------------------------------------
# Import graph + PageRank
# ---------------------------------------------------------------------------

def _resolve_import_to_file(importer_path: str, import_text: str, all_paths: set[str]) -> Optional[str]:
    """
    Best-effort resolution of an import string (e.g. "auth", "./utils",
    "..pkg.thing") to an actual file path IN this project's file set.
    Deliberately conservative: returns None (no edge added) rather than
    guessing wrong -- a missing edge just means slightly less-complete
    ranking data, but a WRONG edge corrupts the whole graph's ranking.
    """
    importer_dir = Path(importer_path).parent

    candidates = []
    if import_text.startswith("."):
        # Relative import (JS "./utils", "../models"; Python ".pkg", "..pkg.thing")
        cleaned = import_text.lstrip(".")
        parts = cleaned.replace(".", "/").split("/") if cleaned else []
        base = importer_dir
        # Python relative imports: leading dots count levels up.
        leading_dots = len(import_text) - len(import_text.lstrip("."))
        for _ in range(max(0, leading_dots - 1)):
            base = base.parent
        candidate_base = base / "/".join(parts) if parts else base
        candidates.extend([
            f"{candidate_base}.py", f"{candidate_base}.js", f"{candidate_base}.jsx",
            f"{candidate_base}/index.js",
        ])
    else:
        # Absolute-ish import (Python "auth.models", JS bare specifiers are
        # usually external packages -- only match if it resolves to a REAL
        # file in this project, otherwise it's assumed external and
        # correctly produces no edge).
        dotted = import_text.replace(".", "/")
        candidates.extend([
            f"{dotted}.py", f"{dotted}.js", f"{dotted}.jsx",
            f"{import_text}.py", f"{import_text}.js",
        ])

    for candidate in candidates:
        normalized = str(Path(candidate)).replace("\\", "/")
        if normalized in all_paths:
            return normalized
        # also try relative to importer's directory for bare relative-style names
        alt = (importer_dir / Path(candidate).name)
        alt_norm = str(alt).replace("\\", "/")
        if alt_norm in all_paths:
            return alt_norm
    return None


def build_import_graph(files: dict[str, FileSymbols]) -> list[tuple[str, str]]:
    """Returns a list of (importer, imported) edges, resolved to real file
    paths within `files` only -- an import that can't be resolved to a
    real project file (e.g. a third-party package) is silently dropped,
    not guessed at (see _resolve_import_to_file's own docstring)."""
    all_paths = set(files.keys())
    edges = []
    for path, symbols in files.items():
        for import_text in symbols.imports:
            resolved = _resolve_import_to_file(path, import_text, all_paths)
            if resolved and resolved != path:
                edges.append((path, resolved))
    return edges


def _pagerank(nodes: list[str], edges: list[tuple[str, str]], alpha: float = 0.85,
              max_iter: int = 100, tol: float = 1.0e-10) -> dict[str, float]:
    """
    Hand-rolled PageRank -- VERIFIED NUMERICALLY against networkx's real
    nx.pagerank() output (see module docstring point 3): matched to 6
    decimal places on both a normal multi-file graph and one containing
    an isolated node (a file with zero imports/importers), which is why
    `nodes` is a REQUIRED separate parameter here rather than derived
    from `edges` alone -- edges can never reveal an isolated file, a real
    gap found during that verification.
    """
    all_nodes = sorted(set(nodes) | {a for a, b in edges} | {b for a, b in edges})
    n = len(all_nodes)
    if n == 0:
        return {}

    out_edges: dict[str, list[str]] = {node: [] for node in all_nodes}
    for a, b in edges:
        out_edges[a].append(b)
    out_degree = {node: len(out_edges[node]) for node in all_nodes}
    rank = {node: 1.0 / n for node in all_nodes}

    for _ in range(max_iter):
        new_rank = {node: (1.0 - alpha) / n for node in all_nodes}
        dangling_sum = sum(rank[node] for node in all_nodes if out_degree[node] == 0)
        for node in all_nodes:
            new_rank[node] += alpha * dangling_sum / n
        for node in all_nodes:
            if out_degree[node] == 0:
                continue
            share = alpha * rank[node] / out_degree[node]
            for target in out_edges[node]:
                new_rank[target] += share

        diff = sum(abs(new_rank[node] - rank[node]) for node in all_nodes)
        rank = new_rank
        if diff < tol:
            break

    return rank


def rank_files(files: dict[str, FileSymbols], query: str = "") -> list[tuple[str, float]]:
    """
    Returns [(path, score), ...] sorted by score descending. Base score is
    PageRank over the resolved import graph; a file whose definitions
    contain a word from `query` gets a modest boost (NOT the proposal's
    50x -- see the "What this deliberately doesn't do" note below) so a
    task-relevant file surfaces higher without one keyword match
    completely overwhelming genuine structural importance.
    """
    edges = build_import_graph(files)
    base_scores = _pagerank(list(files.keys()), edges)

    query_words = {w.lower() for w in re.findall(r"\w+", query) if len(w) > 2}

    boosted = {}
    for path, score in base_scores.items():
        boost = 1.0
        symbols = files.get(path)
        if symbols and query_words:
            symbol_words = {s.lower() for s in symbols.definitions}
            if query_words & symbol_words:
                boost = 3.0  # modest, deliberate -- see note above
        boosted[path] = score * boost

    return sorted(boosted.items(), key=lambda kv: kv[1], reverse=True)


# ---------------------------------------------------------------------------
# Formatting -- token-budgeted output
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """A rough, deliberately conservative estimate (chars/4 is a common
    rule of thumb for English/code text) -- this is advisory budgeting,
    not a hard guarantee, so a rough estimate is fine; the real
    truncation safety net is tools.MAX_TOOL_OUTPUT_CHARS, already applied
    by whatever tool calls this."""
    return max(1, len(text) // 4)


def format_repo_map(ranked: list[tuple[str, float]], files: dict[str, FileSymbols],
                     max_tokens: int = DEFAULT_TOKEN_BUDGET) -> str:
    """Renders the top-ranked files (path + up to MAX_SYMBOLS_PER_FILE_SHOWN
    definitions each) until the token budget is exhausted. Definition
    SIGNATURES only (just names, not implementations) -- matching
    OpenClaude's own documented behavior and Aider's original design:
    the model still needs read_file for actual implementation, this is
    purely a map of what exists and how it connects."""
    lines = []
    used_tokens = 0

    for path, score in ranked:
        symbols = files.get(path)
        if symbols is None:
            continue
        shown_defs = symbols.definitions[:MAX_SYMBOLS_PER_FILE_SHOWN]
        entry_lines = [path] + [f"  {d}" for d in shown_defs]
        entry_text = "\n".join(entry_lines)
        entry_tokens = _estimate_tokens(entry_text)

        if used_tokens + entry_tokens > max_tokens and lines:
            break  # always include at least the top file, even if it alone exceeds budget

        lines.append(entry_text)
        used_tokens += entry_tokens

    return "\n".join(lines)


def get_repo_map(query: str = "", max_tokens: int = DEFAULT_TOKEN_BUDGET,
                  root: Optional[Path] = None, use_cache: bool = True) -> str:
    """The single entry point: scan, rank, format -- everything above
    composed together. Returns an empty string (not an error) if
    REPO_MAP_AVAILABLE is False or the project has no supported files at
    all, so a caller can safely do `if get_repo_map(): ...` without a
    separate availability check."""
    if not REPO_MAP_AVAILABLE:
        return ""
    files = scan_repo(root=root, use_cache=use_cache)
    if not files:
        return ""
    ranked = rank_files(files, query=query)
    return format_repo_map(ranked, files, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# Tool wrapper
# ---------------------------------------------------------------------------

def _tool_repo_map_query(query: str = "", max_tokens: int = DEFAULT_TOKEN_BUDGET) -> str:
    result = get_repo_map(query=query, max_tokens=max_tokens)
    if not result:
        return "(no repo map available -- either tree-sitter isn't installed, or no .py/.js files were found)"
    return result


TOOL_FUNCTIONS = {
    "repo_map_query": _tool_repo_map_query,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "repo_map_query",
            "description": (
                "Get a ranked map of the most structurally important files in this project "
                "(by PageRank over the real import graph, optionally boosted toward files whose "
                "definitions match your query) -- each entry shows a file path and its top "
                "function/class definitions (signatures only, not implementations). Use this "
                "BEFORE diving into list_files/read_file one at a time when you need an overview "
                "of what exists and how files connect, especially in an unfamiliar codebase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional: words related to your task (e.g. 'login authentication') to boost matching files higher in the ranking.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": f"Rough token budget for the returned map. Defaults to {DEFAULT_TOKEN_BUDGET}.",
                    },
                },
                "required": [],
            },
        },
    },
]
