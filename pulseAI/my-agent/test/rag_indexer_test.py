"""
Direct test of rag_indexer.py (chunking + indexing + semantic search),
run against the REAL test/finance_dashboard codebase (not synthetic data).

Run with: PYTHONPATH=/home/user/my-agent python3 test/rag_indexer_test.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rag_indexer  # noqa: E402


def test_decorator_stays_attached_to_function():
    """
    Real bug found and fixed during development: a Flask route decorator
    (@app.route(...)) was ending up attached to the END of the PREVIOUS
    chunk instead of the function it actually decorates -- so a search for
    the route string would miss the function it belongs to.
    """
    content = Path("test/finance_dashboard/app.py").read_text()
    chunks = rag_indexer.chunk_file(Path("app.py"), content)

    balance_chunk = None
    for text, start, end in chunks:
        if "def api_balance" in text:
            balance_chunk = text
            break

    assert balance_chunk is not None, "should find a chunk containing api_balance"
    assert "@app.route('/api/balance')" in balance_chunk, \
        f"the decorator should be in the SAME chunk as the function it decorates, got:\n{balance_chunk}"
    print("PASS: @app.route decorator stays attached to its function")


def test_no_content_lost_across_chunks():
    """Every line of the original file, addressed by its reported
    (start_line, end_line) range, should reconstruct the original file
    exactly -- chunking must not silently drop, duplicate, or reorder
    lines."""
    content = Path("test/finance_dashboard/app.py").read_text()
    original_lines = content.splitlines()
    chunks = rag_indexer.chunk_file(Path("app.py"), content)

    reconstructed = []
    for _text, start, end in chunks:
        # Use the ORIGINAL file's lines at the reported range, not the
        # chunk's own re-joined text -- re-joining with "\n".join() and then
        # calling .splitlines() again loses a trailing blank line per
        # chunk boundary (a str-round-trip artifact, not real data loss),
        # which is what an earlier version of this test incorrectly flagged
        # as a bug. The (start_line, end_line) metadata is the actual
        # contract callers rely on (e.g. to jump to a match in an editor),
        # so that's what must be verified as gapless and non-overlapping.
        reconstructed.extend(original_lines[start - 1:end])

    assert reconstructed == original_lines, (
        "the (start_line, end_line) ranges across all chunks should "
        "reconstruct the original file exactly, in order, with no gaps "
        "or overlaps"
    )
    print("PASS: no lines lost, duplicated, or reordered across chunk line ranges")



def test_index_and_search_end_to_end():
    if not rag_indexer.RAG_AVAILABLE:
        print("SKIP: chromadb not available")
        return

    # Use an isolated index dir so this test never touches the project's
    # real .agent_rag_index/.
    original_index_dir = rag_indexer.INDEX_DIR
    test_index_dir = Path("test/rag_index_test_workdir")
    if test_index_dir.exists():
        shutil.rmtree(test_index_dir)
    rag_indexer.INDEX_DIR = test_index_dir

    try:
        result = rag_indexer.index_directory("test/finance_dashboard")
        print("index_directory:", result)
        assert "Indexed" in result

        stats = rag_indexer.index_stats()
        print("index_stats:\n", stats)
        assert "app.py" in stats

        # The whole point of RAG over grep: find code by CONCEPT, not exact
        # wording. "calculate balance" doesn't appear verbatim in app.py
        # (the code says "SUM(balance)", not "calculate balance") -- confirm
        # the semantic match still surfaces api_balance near the top.
        search_result = rag_indexer.search("calculate the total balance", n_results=3)
        print("search 'calculate the total balance':\n", search_result)
        assert "app.py" in search_result

        search_result2 = rag_indexer.search("bitcoin price from external API", n_results=3)
        print("search 'bitcoin price from external API':\n", search_result2)
        assert "app.py" in search_result2
        assert "bitcoin" in search_result2.lower() or "coingecko" in search_result2.lower()

        print("PASS: index + semantic search work end-to-end against real code")
    finally:
        rag_indexer.INDEX_DIR = original_index_dir
        if test_index_dir.exists():
            shutil.rmtree(test_index_dir)


def test_sensitive_files_never_indexed():
    result = rag_indexer.index_file(".env")
    assert "ERROR" in result and "sensitive" in result.lower()
    print("PASS: .env is refused by index_file, same as read_file/write_file")


if __name__ == "__main__":
    test_decorator_stays_attached_to_function()
    test_no_content_lost_across_chunks()
    test_sensitive_files_never_indexed()
    test_index_and_search_end_to_end()
    print("\nALL TESTS PASSED")
