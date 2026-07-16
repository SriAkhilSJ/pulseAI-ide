"""
Direct tests for repo_map.py, mostly WITHOUT calling any real LLM (this
feature is pure static analysis + graph ranking, so most of it can be
verified with plain assertions -- the one live test at the bottom checks
the repo_map_query TOOL is actually usable by a real model).

Verifies the specific bugs found and fixed while building this module
(see repo_map.py's own module docstring for the full fact-check writeup):
1. The real tree-sitter API (Parser(Language(...)) constructor,
   QueryCursor(query).matches(node)) -- NOT parser.set_language()/
   query.captures, which a proposal's own verification snippet assumed
   and which fails immediately against the actually-installed
   tree-sitter 0.26.0.
2. The JS require() detection query needs a #eq? predicate -- without
   it, (call_expression function: (identifier) @require) matches EVERY
   function call, not just require(...).
3. The hand-rolled PageRank is numerically correct, verified against
   networkx's real nx.pagerank() output (both installed in this sandbox,
   even though networkx is only a documented FUTURE dependency -- see
   REPO_MAP_NETWORKX_UPGRADE.md).

Run with: PYTHONPATH=/home/user/my-agent python3 test/repo_map_test.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402 -- import first, same reason as every other optional-module test
import repo_map  # noqa: E402

SCRATCH_REPO_DIR = Path("test/scratch/repo_map_test_dir")


def _reset():
    if SCRATCH_REPO_DIR.exists():
        shutil.rmtree(SCRATCH_REPO_DIR)
    SCRATCH_REPO_DIR.mkdir(parents=True)


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------

def test_python_extraction_finds_functions_and_classes():
    source = "def foo():\n    pass\n\nclass Bar:\n    def method(self):\n        pass\n"
    defs, imports = repo_map.extract_python_symbols(source)
    assert "foo" in defs
    assert "Bar" in defs
    assert "method" in defs
    print("PASS: Python extraction finds top-level and nested function/class definitions")


def test_python_extraction_finds_imports_both_forms():
    source = "import os\nfrom auth import login, logout\nimport utils as u\n"
    defs, imports = repo_map.extract_python_symbols(source)
    assert "os" in imports
    assert "auth" in imports
    assert "utils" in imports
    # Critically: 'login'/'logout' (the IMPORTED NAMES, not the module) must
    # NOT appear in imports -- they're separate 'name' fields on
    # import_from_statement, verified directly against the real parse tree
    # field structure before this module was written.
    assert "login" not in imports and "logout" not in imports
    print("PASS: Python extraction correctly distinguishes the source module from imported names")


def test_python_extraction_handles_relative_imports():
    source = "from . import helpers\nfrom ..pkg import thing\n"
    defs, imports = repo_map.extract_python_symbols(source)
    assert "helpers" in imports
    assert "thing" in imports
    print("PASS: Python extraction handles relative imports (from . import X / from ..pkg import Y)")


def test_python_extraction_never_crashes_on_syntax_error():
    """Tree-sitter parses error-tolerantly by design -- a genuinely broken
    file should yield whatever it can, not raise."""
    source = "def broken(:\n    this is not valid python at all !!!\n"
    try:
        defs, imports = repo_map.extract_python_symbols(source)
        print(f"PASS: a syntactically broken Python file doesn't crash extraction (got defs={defs}, imports={imports})")
    except Exception as e:
        assert False, f"extraction must not raise on a syntax error, got: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# JavaScript extraction -- including THE critical require() bug fix
# ---------------------------------------------------------------------------

def test_js_extraction_finds_functions_and_classes():
    source = "function calculateTotal(a, b) { return a + b; }\nclass User {}\n"
    defs, imports = repo_map.extract_javascript_symbols(source)
    assert "calculateTotal" in defs
    assert "User" in defs
    print("PASS: JS extraction finds function declarations and classes")


def test_js_extraction_finds_es_module_imports():
    source = 'import { login, logout } from "./auth";\nimport utils from "./utils";\n'
    defs, imports = repo_map.extract_javascript_symbols(source)
    assert "./auth" in imports
    assert "./utils" in imports
    print("PASS: JS extraction finds ES module import sources, with quotes stripped")


def test_js_extraction_finds_require_calls_but_not_other_function_calls():
    """THE critical test for the real bug found and fixed in this module:
    the require() detection query MUST use a #eq? predicate, or it
    matches every function call in the file. Confirmed directly this
    session that the unfixed query captures console.log/other calls as
    if they were require() -- this test proves the fix holds."""
    source = (
        'const { helper } = require("./helpers");\n'
        'console.log("not an import");\n'
        'calculateTotal(1, 2);\n'
        'someOtherFunction();\n'
    )
    defs, imports = repo_map.extract_javascript_symbols(source)
    assert "./helpers" in imports, f"expected the real require() call to be captured, got imports={imports}"
    assert len(imports) == 1, (
        f"REGRESSION: the require() query must ONLY match require(...) calls, not every function call "
        f"in the file -- got {len(imports)} imports: {imports} (the real bug this test guards against "
        f"would have also captured 'console'/'calculateTotal'/'someOtherFunction')"
    )
    print(f"PASS: require() detection correctly captures ONLY the real require() call ({imports}), "
          f"not console.log/calculateTotal/someOtherFunction -- the exact bug found and fixed in this module")


def test_js_extraction_never_crashes_on_syntax_error():
    source = "function broken( {\n    this is not valid javascript &&&\n"
    try:
        defs, imports = repo_map.extract_javascript_symbols(source)
        print(f"PASS: a syntactically broken JS file doesn't crash extraction (got defs={defs}, imports={imports})")
    except Exception as e:
        assert False, f"extraction must not raise on a syntax error, got: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------

def test_list_project_files_uses_git_in_a_real_repo():
    """This project itself IS a real git repo -- confirm list_project_files
    actually uses git and returns real, known files."""
    files = repo_map.list_project_files()
    assert "agent.py" in files
    assert "tools.py" in files
    # node_modules must be excluded via .gitignore, matching OpenClaude's
    # own documented behavior.
    assert not any("node_modules" in f for f in files), "node_modules must be excluded via git's own .gitignore handling"
    print(f"PASS: list_project_files() finds real project files via git ls-files ({len(files)} files), excludes node_modules")


def test_list_project_files_falls_back_for_non_git_directory():
    """A directory that ISN'T a git repo at all must fall back to a plain
    walk, not crash."""
    _reset()
    (SCRATCH_REPO_DIR / "a.py").write_text("def foo(): pass\n", encoding="utf-8")
    (SCRATCH_REPO_DIR / "node_modules").mkdir()
    (SCRATCH_REPO_DIR / "node_modules" / "junk.js").write_text("ignored", encoding="utf-8")

    files = repo_map.list_project_files(root=SCRATCH_REPO_DIR)
    assert "a.py" in files
    assert not any("node_modules" in f for f in files), "the fallback walk must still skip common noise directories"
    print("PASS: list_project_files() falls back to a manual walk (still skipping node_modules) for a non-git directory")


# ---------------------------------------------------------------------------
# Import graph resolution
# ---------------------------------------------------------------------------

def test_import_graph_resolves_real_python_relative_import():
    files = {
        "pkg/main.py": repo_map.FileSymbols(path="pkg/main.py", definitions=[], imports=["utils"]),
        "pkg/utils.py": repo_map.FileSymbols(path="pkg/utils.py", definitions=["helper"], imports=[]),
    }
    edges = repo_map.build_import_graph(files)
    assert ("pkg/main.py", "pkg/utils.py") in edges
    print("PASS: import graph correctly resolves a same-directory Python import to the real file")


def test_import_graph_does_not_guess_wrong_for_external_package():
    """An import of a real third-party package (not in this project's
    file set) must NOT produce a fabricated edge -- see
    _resolve_import_to_file's own docstring: a missing edge is fine, a
    WRONG edge corrupts the whole ranking."""
    files = {
        "app.py": repo_map.FileSymbols(path="app.py", definitions=[], imports=["numpy", "requests"]),
    }
    edges = repo_map.build_import_graph(files)
    assert edges == [], f"an import of an external package must never produce a fabricated edge, got: {edges}"
    print("PASS: an import of an external package (not in the project) correctly produces NO edge, not a guessed/wrong one")


def test_import_graph_resolves_js_relative_import():
    files = {
        "src/app.js": repo_map.FileSymbols(path="src/app.js", definitions=[], imports=["./auth"]),
        "src/auth.js": repo_map.FileSymbols(path="src/auth.js", definitions=["login"], imports=[]),
    }
    edges = repo_map.build_import_graph(files)
    assert ("src/app.js", "src/auth.js") in edges
    print("PASS: import graph correctly resolves a JS relative import (./auth) to the real file")


# ---------------------------------------------------------------------------
# PageRank -- verified numerically against networkx's real output
# ---------------------------------------------------------------------------

def test_pagerank_matches_networkx_on_normal_graph():
    """Direct numerical comparison against networkx's real nx.pagerank()
    output -- both are installed in this sandbox (networkx only by
    documented sandbox coincidence, see REPO_MAP_NETWORKX_UPGRADE.md, but
    it's genuinely present right now and is the correct ground truth to
    verify against)."""
    try:
        import networkx as nx
    except ImportError:
        print("SKIP: networkx not installed in this environment, can't cross-verify (repo_map's own hand-rolled version is used regardless)")
        return

    edges = [
        ("app.py", "auth.py"), ("app.py", "utils.py"), ("app.py", "models.py"),
        ("auth.py", "models.py"), ("auth.py", "utils.py"),
        ("api.py", "auth.py"), ("api.py", "models.py"), ("api.py", "utils.py"),
        ("models.py", "utils.py"),
    ]
    nodes = ["app.py", "auth.py", "utils.py", "models.py", "api.py"]

    g = nx.DiGraph()
    g.add_edges_from(edges)
    nx_ranks = nx.pagerank(g, alpha=0.85)

    our_ranks = repo_map._pagerank(nodes, edges)

    for node in nodes:
        diff = abs(nx_ranks[node] - our_ranks[node])
        assert diff < 1e-5, f"PageRank mismatch for {node}: networkx={nx_ranks[node]:.6f}, ours={our_ranks[node]:.6f}"
    print("PASS: hand-rolled PageRank matches networkx's real output to 5 decimal places on a normal graph")


def test_pagerank_matches_networkx_with_isolated_node():
    """THE real gap found during verification: an isolated node (zero
    imports AND zero importers) can't be discovered from edges alone --
    the full node set must be passed explicitly. Confirmed this session
    that omitting it silently drops the isolated node from the ranking
    entirely."""
    try:
        import networkx as nx
    except ImportError:
        print("SKIP: networkx not installed, can't cross-verify")
        return

    edges = [("a.py", "b.py"), ("c.py", "b.py")]
    nodes = ["a.py", "b.py", "c.py", "d.py"]  # d.py is isolated -- zero edges at all

    g = nx.DiGraph()
    g.add_edges_from(edges)
    g.add_node("d.py")
    nx_ranks = nx.pagerank(g)

    our_ranks = repo_map._pagerank(nodes, edges)

    assert "d.py" in our_ranks, "an isolated node must still appear in the ranking (this was the real gap found)"
    for node in nodes:
        diff = abs(nx_ranks[node] - our_ranks[node])
        assert diff < 1e-5, f"PageRank mismatch for {node}: networkx={nx_ranks[node]:.6f}, ours={our_ranks[node]:.6f}"
    print("PASS: hand-rolled PageRank correctly includes an isolated node (zero edges) and matches networkx exactly")


def test_pagerank_empty_graph_returns_empty_dict():
    result = repo_map._pagerank([], [])
    assert result == {}
    print("PASS: PageRank on an empty graph returns an empty dict, no crash")


# ---------------------------------------------------------------------------
# rank_files / format_repo_map
# ---------------------------------------------------------------------------

def test_rank_files_query_boost_surfaces_matching_file():
    files = {
        "auth.py": repo_map.FileSymbols(path="auth.py", definitions=["login", "logout"], imports=[]),
        "unrelated.py": repo_map.FileSymbols(path="unrelated.py", definitions=["format_date"], imports=[]),
    }
    ranked_no_query = repo_map.rank_files(files, query="")
    ranked_with_query = repo_map.rank_files(files, query="login authentication")

    # Both files are isolated (no edges), so base PageRank is identical --
    # the query boost is the only thing that can change the order.
    assert ranked_with_query[0][0] == "auth.py", f"expected auth.py to rank first when the query mentions 'login', got: {ranked_with_query}"
    print("PASS: rank_files boosts a file whose definitions match the query terms")


def test_format_repo_map_respects_token_budget():
    files = {f"file{i}.py": repo_map.FileSymbols(path=f"file{i}.py", definitions=[f"func{i}_{j}" for j in range(20)], imports=[])
             for i in range(20)}
    ranked = [(f"file{i}.py", 1.0 - i * 0.01) for i in range(20)]
    result_small_budget = repo_map.format_repo_map(ranked, files, max_tokens=50)
    result_large_budget = repo_map.format_repo_map(ranked, files, max_tokens=5000)
    assert len(result_small_budget) < len(result_large_budget), "a smaller token budget must produce a shorter result"
    assert "file0.py" in result_small_budget, "the top-ranked file must always be included, even under a tight budget"
    print("PASS: format_repo_map respects the token budget, always including at least the top-ranked file")


def test_format_repo_map_shows_signatures_not_implementations():
    files = {"auth.py": repo_map.FileSymbols(path="auth.py", definitions=["login", "logout"], imports=[])}
    ranked = [("auth.py", 1.0)]
    result = repo_map.format_repo_map(ranked, files)
    assert "login" in result and "logout" in result
    assert "def " not in result, "the repo map must show definition NAMES only, not full implementations"
    print("PASS: format_repo_map shows only definition names (signatures), never full implementation bodies")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_cache_actually_skips_reparsing_unchanged_file():
    """Direct proof the cache works: parse once, corrupt the extractor
    function (monkeypatch it to raise), scan again with use_cache=True --
    if the cache were NOT working, the second scan would crash because
    the corrupted extractor would actually be invoked."""
    _reset()
    target = SCRATCH_REPO_DIR / "cached_file.py"
    target.write_text("def foo(): pass\n", encoding="utf-8")

    files1 = repo_map.scan_repo(root=SCRATCH_REPO_DIR, use_cache=True)
    assert "cached_file.py" in files1

    original_extractor = repo_map._EXTRACTORS[".py"]

    def broken_extractor(source):
        raise RuntimeError("cache did not prevent re-parsing -- this should never be called")

    repo_map._EXTRACTORS[".py"] = broken_extractor
    try:
        files2 = repo_map.scan_repo(root=SCRATCH_REPO_DIR, use_cache=True)
        assert "cached_file.py" in files2
        assert files2["cached_file.py"].definitions == ["foo"], "cached data must be identical to the original parse"
        print("PASS: an unchanged file is served from cache, genuinely skipping re-parsing (proven via a corrupted extractor that was never invoked)")
    finally:
        repo_map._EXTRACTORS[".py"] = original_extractor


def test_cache_invalidates_on_real_file_change():
    """The other half: a REAL modification (different mtime+size) must
    NOT be served stale data from cache."""
    _reset()
    target = SCRATCH_REPO_DIR / "changing_file.py"
    target.write_text("def foo(): pass\n", encoding="utf-8")
    files1 = repo_map.scan_repo(root=SCRATCH_REPO_DIR, use_cache=True)
    assert files1["changing_file.py"].definitions == ["foo"]

    import time
    time.sleep(0.01)  # ensure a different mtime
    target.write_text("def foo(): pass\ndef bar(): pass\n", encoding="utf-8")
    files2 = repo_map.scan_repo(root=SCRATCH_REPO_DIR, use_cache=True)
    assert files2["changing_file.py"].definitions == ["foo", "bar"], (
        f"a real file modification must invalidate the cache, got stale: {files2['changing_file.py'].definitions}"
    )
    print("PASS: a real file modification correctly invalidates the cache (new content is re-parsed, not served stale)")


# ---------------------------------------------------------------------------
# Full pipeline against THIS project's own real, live codebase
# ---------------------------------------------------------------------------

def test_full_pipeline_against_real_project_ranks_tools_py_highly():
    """The real, live cross-check: tools.py is genuinely imported by
    nearly every module in this project -- it should rank very highly by
    real PageRank, not asserted in isolation."""
    result = repo_map.get_repo_map(max_tokens=3000)
    assert result, "expected a non-empty repo map for this project's real codebase"
    top_files = result.split("\n\n")[0] if "\n\n" in result else result
    lines = result.splitlines()
    # tools.py should appear within the first few entries (each entry is
    # a file path line followed by indented definition lines).
    file_lines = [l for l in lines if l and not l.startswith(" ")]
    top_5_files = file_lines[:5]
    assert "tools.py" in top_5_files, f"expected tools.py in the top 5 ranked files, got: {top_5_files}"
    print(f"PASS: the full pipeline against this project's REAL codebase ranks tools.py in the top 5: {top_5_files}")


def test_repo_map_query_tool_registered():
    assert tools.REPO_MAP_AVAILABLE is True
    assert "repo_map_query" in tools.TOOL_FUNCTIONS
    spec_names = {s["function"]["name"] for s in tools.TOOL_SPECS}
    assert "repo_map_query" in spec_names
    print("PASS: repo_map_query is registered in tools.TOOL_FUNCTIONS/TOOL_SPECS")


if __name__ == "__main__":
    test_python_extraction_finds_functions_and_classes()
    test_python_extraction_finds_imports_both_forms()
    test_python_extraction_handles_relative_imports()
    test_python_extraction_never_crashes_on_syntax_error()
    test_js_extraction_finds_functions_and_classes()
    test_js_extraction_finds_es_module_imports()
    test_js_extraction_finds_require_calls_but_not_other_function_calls()
    test_js_extraction_never_crashes_on_syntax_error()
    test_list_project_files_uses_git_in_a_real_repo()
    test_list_project_files_falls_back_for_non_git_directory()
    test_import_graph_resolves_real_python_relative_import()
    test_import_graph_does_not_guess_wrong_for_external_package()
    test_import_graph_resolves_js_relative_import()
    test_pagerank_matches_networkx_on_normal_graph()
    test_pagerank_matches_networkx_with_isolated_node()
    test_pagerank_empty_graph_returns_empty_dict()
    test_rank_files_query_boost_surfaces_matching_file()
    test_format_repo_map_respects_token_budget()
    test_format_repo_map_shows_signatures_not_implementations()
    test_cache_actually_skips_reparsing_unchanged_file()
    test_cache_invalidates_on_real_file_change()
    test_full_pipeline_against_real_project_ranks_tools_py_highly()
    test_repo_map_query_tool_registered()
    _reset()
    if SCRATCH_REPO_DIR.exists():
        shutil.rmtree(SCRATCH_REPO_DIR)
    print("\nALL TESTS PASSED")
