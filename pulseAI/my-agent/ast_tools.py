"""
ast_tools.py
------------
Surgical code transforms via Tree-sitter, for changes that go beyond what
LSP's rename/reference tools (tools_lsp.py) do: e.g. "convert every safely-
convertible `var` to `const`" or "find every function missing a JSDoc
comment" -- LSP servers don't expose this kind of custom, rule-based
codebase transform. Currently covers JavaScript; tree-sitter-python is
installed and available for future Python-specific transforms but nothing
uses it yet (no Python transform was requested).

IMPORTANT -- this module was fact-checked against the ACTUALLY INSTALLED
tree-sitter 0.26.0 / tree-sitter-javascript 0.25.0 before writing any of
this, because a proposed design (not implemented as given) had FOUR real
bugs, each individually confirmed by running code against the real
library first:

  1. `Language.query(...)` does not exist on tree-sitter 0.26 -- confirmed
     `AttributeError: 'tree_sitter.Language' object has no attribute
     'query'`. The real API is `Query(language, pattern_string)` to build
     a query, then `QueryCursor(query).matches(node)` to run it -- Query
     objects don't have a `.matches()` method themselves either (confirmed
     `hasattr(Query(...), 'matches') == False`).
  2. `"(var_declaration) @var"` is not a valid node type in this grammar --
     confirmed `tree_sitter.QueryError: Invalid node type ... var_declaration`.
     Walked the actual parse tree of `var x = 1; let y = 2; const z = 3;`
     directly and confirmed the real node types: `var` produces
     `variable_declaration`, while `let`/`const` produce a DIFFERENT node
     type, `lexical_declaration`.
  3. Match objects are NOT positional tuples indexable like `match[0][0]`
     or `match[2][0]` -- confirmed each match is `(match_index, captures)`
     where `captures` is a dict keyed by capture NAME (e.g.
     `captures["name"][0]`), not a list you can index by capture position.
  4. THE SERIOUS ONE: reassignment detection based only on
     `assignment_expression` (`x = 2`) misses two other real, distinct
     node types that ALSO mutate a variable -- confirmed by parsing
     `a += 1` and `b++` directly: they produce
     `augmented_assignment_expression` and `update_expression`
     respectively, neither of which is an `assignment_expression`. Missing
     these means a `var` that's later incremented (`counter++`) or
     compound-assigned (`total += tax`) would be converted to `const`,
     which is not just "unsafe" but a GUARANTEED JavaScript runtime crash
     (`TypeError: Assignment to constant variable`) in the resulting code
     -- actively worse than not transforming it at all. Fixed by checking
     all three mutation node types, not just one.

Like git_tools.py and rag_indexer.py, this module does NOT import `tools`
at module level -- it takes/returns plain strings (file content in,
transformed content out) and lets the CALLER (tools.py, or a human) do
path resolution and file I/O via the existing safe write_file. This also
sidesteps the exact circular-import bug found and fixed in those two
modules (see their docstrings) since this module doesn't need `tools` at
all -- it's pure string-in, string-out.
"""

from __future__ import annotations

from typing import Optional

try:
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_javascript as _tsjs
    AST_TOOLS_AVAILABLE = True
except Exception:
    AST_TOOLS_AVAILABLE = False

_JS_LANGUAGE = None


def _get_js_language():
    global _JS_LANGUAGE
    if _JS_LANGUAGE is None:
        _JS_LANGUAGE = Language(_tsjs.language())
    return _JS_LANGUAGE


def _parser() -> "Parser":
    return Parser(_get_js_language())


def _query(pattern: str) -> "Query":
    return Query(_get_js_language(), pattern)


def _matches(pattern: str, node) -> list:
    """Run a tree-sitter query against `node`, returning the real
    (match_index, {capture_name: [nodes]}) shape -- see module docstring
    point 3 for why this wrapper exists (to have ONE place that gets the
    real API right, instead of repeating QueryCursor(...).matches(...)
    at every call site)."""
    cursor = QueryCursor(_query(pattern))
    return cursor.matches(node)


# Node types that represent a variable being MUTATED after its initial
# declaration. Confirmed via direct parsing (see module docstring point 4)
# that these are three DISTINCT node types in tree-sitter-javascript's
# grammar -- checking only `assignment_expression` misses compound
# assignment (`+=`, `-=`, etc.) and increment/decrement (`++`, `--`).
_MUTATION_QUERIES = [
    "(assignment_expression left: (identifier) @id)",
    "(augmented_assignment_expression left: (identifier) @id)",
    "(update_expression argument: (identifier) @id)",
]


def _find_all_mutated_identifiers(root) -> set:
    """Every identifier name that is reassigned/mutated ANYWHERE in the
    file via `=`, `+=`/`-=`/etc., or `++`/`--`."""
    mutated = set()
    for pattern in _MUTATION_QUERIES:
        for _idx, captures in _matches(pattern, root):
            for node in captures.get("id", []):
                mutated.add(node.text.decode())
    return mutated


def transform_var_to_const_safe(source: str) -> str:
    """
    Convert `var NAME = ...;` declarations to `const NAME = ...;` ONLY
    where NAME is never mutated anywhere else in the file (via `=`, `+=`
    and friends, or `++`/`--` -- see module docstring point 4 for why all
    three matter). Multi-declarator statements (`var a = 1, b = 2;`) are
    only converted if EVERY declared name in that statement is safe;
    otherwise the whole statement is left as `var` (JS doesn't allow mixing
    `const`/`var` semantics within a single declaration statement's syntax
    the way this would require).

    Takes and returns plain source text -- caller is responsible for
    reading/writing the actual file via the existing safe read_file/
    write_file tools.
    """
    if not AST_TOOLS_AVAILABLE:
        raise RuntimeError("AST tools are not available (tree-sitter not installed).")

    tree = _parser().parse(source.encode())
    root = tree.root_node

    mutated_names = _find_all_mutated_identifiers(root)

    var_decl_matches = _matches("(variable_declaration) @var", root)
    if not var_decl_matches:
        return source

    edits = []  # (start_byte, end_byte, replacement_text)
    for _idx, captures in var_decl_matches:
        var_node = captures["var"][0]

        # Find every name declared in THIS var statement (handles
        # multi-declarator: `var a = 1, b = 2;`).
        declarator_matches = _matches("(variable_declarator name: (identifier) @name)", var_node)
        declared_names = [c["name"][0].text.decode() for _i, c in declarator_matches]

        if not declared_names:
            continue  # shouldn't happen, but don't guess if it does

        if any(name in mutated_names for name in declared_names):
            continue  # at least one name in this statement is mutated -- leave the whole statement as `var`

        # The `var` keyword is always the FIRST child token of a
        # variable_declaration node (confirmed directly: node.children[0].type == "var").
        var_keyword_node = var_node.children[0]
        if var_keyword_node.type != "var":
            continue  # defensive -- shouldn't happen given the query, but never guess-replace blindly
        edits.append((var_keyword_node.start_byte, var_keyword_node.end_byte, "const"))

    if not edits:
        return source

    # Apply edits in reverse byte-offset order so earlier edits' positions
    # aren't invalidated by later ones changing the string length.
    result = source
    source_bytes = source.encode()
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        source_bytes = source_bytes[:start] + replacement.encode() + source_bytes[end:]
    return source_bytes.decode()


def add_jsdoc_to_function(source: str, function_name: str, params: dict, returns: str = "void") -> str:
    """
    Insert a JSDoc comment immediately before the named top-level function
    declaration. `params` maps parameter name -> type string, e.g.
    {"amount": "number", "tax": "number"}.

    Raises ValueError if the function isn't found (a top-level
    function_declaration with that exact name) -- does not guess or fall
    back to a partial match.
    """
    if not AST_TOOLS_AVAILABLE:
        raise RuntimeError("AST tools are not available (tree-sitter not installed).")

    tree = _parser().parse(source.encode())
    root = tree.root_node

    # #eq? predicate confirmed working directly against the real API
    # (see module docstring) -- filters to only the function with this
    # exact name, not a substring/prefix match.
    pattern = f'(function_declaration name: (identifier) @name (#eq? @name "{function_name}")) @func'
    matches = _matches(pattern, root)
    if not matches:
        raise ValueError(f"No top-level function named {function_name!r} found.")

    func_node = matches[0][1]["func"][0]

    jsdoc_lines = ["/**"]
    for param, ptype in params.items():
        jsdoc_lines.append(f" * @param {{{ptype}}} {param}")
    jsdoc_lines.append(f" * @returns {{{returns}}}")
    jsdoc_lines.append(" */")
    jsdoc = "\n".join(jsdoc_lines) + "\n"

    source_bytes = source.encode()
    insert_pos = func_node.start_byte
    result_bytes = source_bytes[:insert_pos] + jsdoc.encode() + source_bytes[insert_pos:]
    return result_bytes.decode()


def find_untyped_functions(source: str) -> list[dict]:
    """
    Find top-level JS functions with no JSDoc comment immediately above
    them. Heuristic (checks the previous non-empty source line for a
    JSDoc-closing `*/`), not a semantic guarantee -- e.g. it won't detect
    a JSDoc block separated from the function by a blank line. Good enough
    for "find candidates to add docs to", not a strict linter.
    """
    if not AST_TOOLS_AVAILABLE:
        raise RuntimeError("AST tools are not available (tree-sitter not installed).")

    tree = _parser().parse(source.encode())
    root = tree.root_node
    lines = source.splitlines()

    pattern = """
    (function_declaration
      name: (identifier) @name
      parameters: (formal_parameters) @params) @func
    """
    matches = _matches(pattern, root)

    untyped = []
    for _idx, captures in matches:
        func_node = captures["func"][0]
        name_node = captures["name"][0]
        params_node = captures["params"][0]

        start_line = func_node.start_point[0]  # 0-indexed
        has_jsdoc = False
        if start_line > 0:
            prev_line = lines[start_line - 1].strip()
            has_jsdoc = prev_line.endswith("*/")

        if not has_jsdoc:
            untyped.append({
                "name": name_node.text.decode(),
                "line": start_line + 1,  # report 1-indexed to match editor conventions
                "params": params_node.text.decode(),
            })

    return untyped
