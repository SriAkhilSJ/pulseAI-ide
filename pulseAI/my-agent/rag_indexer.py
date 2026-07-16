"""
rag_indexer.py
--------------
Semantic ("concept") search over the codebase, on top of the existing
grep_files (exact text) and lsp_find_references (exact symbol) tools.
Answers queries like "find where we handle OAuth token refresh" that don't
share exact wording with the code, which neither grep nor LSP can do.

Cost/footprint decision (deliberately NOT the originally proposed stack):
the original proposal used `sentence-transformers`, which pulls in `torch`
-- confirmed directly that torch's Linux wheel alone is 507MB, and just
downloading it filled this sandbox's entire 996MB /tmp tmpfs and failed
with "No space left on device". Using ChromaDB's own DEFAULT embedding
function instead: a small ONNX all-MiniLM-L6-v2 model, downloaded once
(~79MB, confirmed directly) to `~/.cache/chroma/onnx_models/`, backed by
`onnxruntime` (already a chromadb dependency, no separate install). Same
embedding-quality class (it's the same MiniLM base model), a fraction of
the footprint, and no GPU/torch dependency chain at all.

Chunking: a proposed design split every file into blind 30-line windows or
blank-line breaks, which routinely cuts a function's signature from its
body or its docstring from its code -- bad for semantic search, since the
embedded text loses the very context that makes it findable by concept.
Instead this chunks along actual function/class boundaries for Python and
JS/TS (regex-based, not a full parser -- good enough for chunk boundaries,
not meant to replace tools_lsp.py's real semantic analysis) and falls back
to fixed-size line windows only for files/languages it doesn't recognize.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

try:
    import chromadb
    RAG_AVAILABLE = True
except Exception:
    RAG_AVAILABLE = False

# NOTE: `tools` is imported LAZILY (inside _get_tools(), on first actual use)
# rather than at module level. Real, reproduced circular-import bug: tools.py
# imports rag_indexer at its own end (to register rag_* as agent tools),
# while this module needs tools.WORKDIR/tools.is_sensitive_path/tools._resolve.
# A module-level `import tools as _tools` here means that if ANYTHING
# imports rag_indexer.py (or git_tools.py, same issue, fixed the same way)
# BEFORE ever importing tools.py, Python has to load tools.py mid-way
# through loading rag_indexer.py -- tools.py's own `from rag_indexer import
# RAG_AVAILABLE, ...` then hits a partially-initialized rag_indexer module
# that doesn't have those names defined yet, an ImportError that tools.py's
# broad `except Exception` swallows SILENTLY, leaving RAG_AVAILABLE=False
# for the rest of that process with no visible error at all. Reproduced
# directly: `import rag_indexer; rag_indexer.INDEX_DIR = <custom path>` (a
# completely reasonable thing to do, e.g. to isolate a test) followed by
# `import agent` showed zero rag_* tools registered, silently. Deferring the
# `tools` import to first actual function call avoids the cycle -- by the
# time any of these functions RUNS, both modules have finished loading.
_tools = None


def _get_tools():
    global _tools
    if _tools is None:
        import tools as _tools_module
        _tools = _tools_module
    return _tools


# INDEX_DIR is a public, directly-settable attribute (e.g. tests reassign it
# to isolate an index location) -- kept as None until first resolved, so
# setting it BEFORE any rag_* call still works exactly as before, but
# computing its default no longer forces `tools` to load at rag_indexer
# import time.
INDEX_DIR: Optional[Path] = None


def _get_index_dir() -> Path:
    global INDEX_DIR
    if INDEX_DIR is None:
        INDEX_DIR = _get_tools().WORKDIR / ".agent_rag_index"
    return INDEX_DIR

# File types worth indexing at all -- skip binaries, images, databases, etc.
# (checked by extension, not content-sniffing, to keep this fast).
_INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".md", ".json",
    ".sh", ".yaml", ".yml", ".txt",
}

# Top-level def/class/function boundaries, used to chunk Python and JS/TS
# files along real code structure instead of blind line windows. Deliberately
# simple (line-anchored regex, not a real parser) -- see tools_lsp.py for
# actual semantic analysis; this only needs "good enough" chunk boundaries.
_PY_BOUNDARY = re.compile(r"^(def |class |async def )", re.MULTILINE)
_JS_BOUNDARY = re.compile(
    r"^(function |class |const \w+\s*=\s*(async\s*)?\(.*\)\s*=>|export (default )?function |export (default )?class )",
    re.MULTILINE,
)

_MAX_CHUNK_CHARS = 2000  # keep chunks small enough to embed meaningfully and cheaply
_FALLBACK_WINDOW_LINES = 40


def _chunk_by_boundaries(content: str, boundary_re: "re.Pattern") -> list[tuple[str, int, int]]:
    """Split `content` into (chunk_text, start_line_1indexed, end_line_1indexed)
    at each top-level boundary match. Any content before the first boundary
    (imports, module docstring) becomes its own leading chunk.

    Real bug found and fixed while testing this against
    test/finance_dashboard/app.py: a Flask route like
        @app.route('/api/balance')
        def api_balance(): ...
    got split with the `@app.route(...)` decorator line left attached to
    the END of the PREVIOUS chunk, and the function itself starting its own
    chunk with no decorator -- so a search for "balance endpoint" would only
    match the function body, losing the literal route string ("/api/balance")
    that's the most useful signal for exactly this kind of query. Fixed by
    walking backward from each boundary match to also swallow any
    contiguous decorator lines (`@...`) immediately preceding it, so the
    decorator stays attached to the function/class it belongs to.
    """
    lines = content.splitlines()
    match_line_indices = []
    for m in boundary_re.finditer(content):
        line_idx = content.count("\n", 0, m.start())
        # Walk backward over contiguous decorator lines (@foo(...), possibly
        # multi-line but we only handle the common single-line case here)
        # so they stay attached to the def/class that follows them.
        while line_idx > 0 and lines[line_idx - 1].strip().startswith("@"):
            line_idx -= 1
        match_line_indices.append(line_idx)

    if not match_line_indices:
        return []

    # Decorator-walking can produce duplicate/out-of-order boundaries if two
    # matches share the same decorator run (shouldn't normally happen, but
    # dedupe+sort defensively rather than trust regex match order blindly).
    match_line_indices = sorted(set(match_line_indices))

    boundaries = [0] + match_line_indices + [len(lines)]
    chunks = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        if start >= end:
            continue
        text = "\n".join(lines[start:end])
        if text.strip():
            # Cap very large chunks (e.g. a huge class) instead of embedding
            # something so long the embedding loses precision.
            if len(text) > _MAX_CHUNK_CHARS:
                text = text[:_MAX_CHUNK_CHARS]
            chunks.append((text, start + 1, end))
    return chunks


def _chunk_fixed_windows(content: str) -> list[tuple[str, int, int]]:
    lines = content.splitlines()
    chunks = []
    for i in range(0, len(lines), _FALLBACK_WINDOW_LINES):
        window = lines[i:i + _FALLBACK_WINDOW_LINES]
        text = "\n".join(window)
        if text.strip():
            chunks.append((text, i + 1, min(i + _FALLBACK_WINDOW_LINES, len(lines))))
    return chunks


def chunk_file(path: Path, content: str) -> list[tuple[str, int, int]]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        chunks = _chunk_by_boundaries(content, _PY_BOUNDARY)
        if chunks:
            return chunks
    elif suffix in (".js", ".jsx", ".ts", ".tsx"):
        chunks = _chunk_by_boundaries(content, _JS_BOUNDARY)
        if chunks:
            return chunks
    return _chunk_fixed_windows(content)


def _get_collection():
    client = chromadb.PersistentClient(path=str(_get_index_dir()))
    return client.get_or_create_collection("codebase")


def index_file(relative_path: str) -> str:
    """(Re)index a single file: chunk it, embed each chunk, upsert into the
    collection. Safe to call repeatedly (upsert, not insert) -- e.g. after
    every write_file, to keep the index current without a separate 'rebuild
    everything' step for a one-file change."""
    if not RAG_AVAILABLE:
        return "ERROR: RAG index is not available (chromadb not installed)."
    if _get_tools().is_sensitive_path(relative_path):
        return f"ERROR: refusing to index a sensitive path: {relative_path}"

    full_path = _get_tools()._resolve(relative_path)
    if not full_path.exists() or not full_path.is_file():
        return f"ERROR: file not found: {relative_path}"
    if full_path.suffix.lower() not in _INDEXABLE_EXTENSIONS:
        return f"Skipped (not an indexable file type): {relative_path}"

    try:
        content = full_path.read_text(errors="ignore")
    except Exception as e:
        return f"ERROR reading {relative_path}: {e}"

    chunks = chunk_file(full_path, content)
    if not chunks:
        return f"No content to index in {relative_path}"

    coll = _get_collection()

    # Remove any existing chunks for this file first (a file can shrink --
    # e.g. from 5 chunks down to 2 -- and stale chunk IDs from the old,
    # longer version would otherwise linger in the index forever).
    coll.delete(where={"file": relative_path})

    ids, documents, metadatas = [], [], []
    for text, start_line, end_line in chunks:
        chunk_id = hashlib.sha256(f"{relative_path}:{start_line}:{end_line}".encode()).hexdigest()[:16]
        ids.append(chunk_id)
        documents.append(text)
        metadatas.append({
            "file": relative_path,
            "line_start": start_line,
            "line_end": end_line,
        })

    coll.upsert(ids=ids, documents=documents, metadatas=metadatas)
    return f"Indexed {relative_path}: {len(chunks)} chunk(s)."


def index_directory(directory: str = ".") -> str:
    """Index every indexable file under `directory` (recursive). Skips
    sensitive paths and common noise directories (node_modules, __pycache__,
    .git, the RAG index's own storage dir, etc.)."""
    if not RAG_AVAILABLE:
        return "ERROR: RAG index is not available (chromadb not installed)."

    root = _get_tools()._resolve(directory)
    if not root.exists():
        return f"ERROR: directory not found: {directory}"

    skip_dir_names = {
        "node_modules", "__pycache__", ".git", ".venv", "venv",
        ".agent_backups", ".agent_missions", ".agent_rag_index",
        ".cache", "dist", "build", ".pytest_cache",
    }

    indexed_count = 0
    skipped_count = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip_dir_names for part in p.relative_to(_get_tools().WORKDIR).parts):
            continue
        rel = str(p.relative_to(_get_tools().WORKDIR))
        if _get_tools().is_sensitive_path(rel):
            skipped_count += 1
            continue
        if p.suffix.lower() not in _INDEXABLE_EXTENSIONS:
            continue
        result = index_file(rel)
        if result.startswith("Indexed"):
            indexed_count += 1
        else:
            skipped_count += 1

    return f"Indexed {indexed_count} file(s), skipped {skipped_count} (sensitive/empty/unsupported)."


def search(query: str, n_results: int = 5) -> str:
    """Semantic search over the indexed codebase. Returns file/line/preview
    for the closest-matching chunks by embedding distance (lower = more
    similar). Complements grep_files (exact text) and lsp_find_references
    (exact symbol) for concept-level queries neither can answer."""
    if not RAG_AVAILABLE:
        return "ERROR: RAG index is not available (chromadb not installed)."

    coll = _get_collection()
    if coll.count() == 0:
        return (
            "The RAG index is empty. Run rag_index_directory first "
            "(e.g. with directory='.') before searching."
        )

    results = coll.query(query_texts=[query], n_results=min(n_results, coll.count()))
    ids = results.get("ids", [[]])[0]
    if not ids:
        return "No results."

    metadatas = results["metadatas"][0]
    documents = results["documents"][0]
    distances = results["distances"][0]

    lines = [f"Top {len(ids)} semantic match(es) for: {query!r}"]
    for meta, doc, dist in zip(metadatas, documents, distances):
        preview = doc[:200].replace("\n", " ")
        lines.append(
            f"\n[{meta['file']}:{meta['line_start']}-{meta['line_end']}] (distance={dist:.3f})\n  {preview}"
        )
    return "\n".join(lines)


def index_stats() -> str:
    """Report how many chunks/files are currently indexed, so the agent can
    check freshness before trusting a search result."""
    if not RAG_AVAILABLE:
        return "ERROR: RAG index is not available (chromadb not installed)."
    coll = _get_collection()
    count = coll.count()
    if count == 0:
        return "Index is empty. Run rag_index_directory first."
    all_items = coll.get()
    files = sorted(set(m["file"] for m in all_items["metadatas"]))
    return f"{count} chunk(s) indexed across {len(files)} file(s):\n" + "\n".join(f"  {f}" for f in files)


# ---------------------------------------------------------------------------
# Agent-callable tool wrappers + specs (registered into tools.py's
# TOOL_FUNCTIONS/TOOL_SPECS only if RAG_AVAILABLE).
# ---------------------------------------------------------------------------

def _tool_rag_index_directory(directory: str = ".") -> str:
    return index_directory(directory)


def _tool_rag_index_file(path: str) -> str:
    return index_file(path)


def _tool_rag_search(query: str, n_results: int = 5) -> str:
    return search(query, n_results)


def _tool_rag_index_stats() -> str:
    return index_stats()


TOOL_FUNCTIONS = {
    "rag_index_directory": _tool_rag_index_directory,
    "rag_index_file": _tool_rag_index_file,
    "rag_search": _tool_rag_search,
    "rag_index_stats": _tool_rag_index_stats,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "rag_index_directory",
            "description": (
                "Build/refresh a semantic search index over every indexable file in a "
                "directory (recursive). Run this once before rag_search on a codebase "
                "you haven't indexed yet, or after major changes across many files. "
                "For a single changed file, prefer rag_index_file (cheaper)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory to index, recursively (default '.', the whole project).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_index_file",
            "description": (
                "(Re)index a single file after you've changed it with write_file, so "
                "rag_search reflects the new content immediately without re-indexing "
                "the whole directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to index."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": (
                "Semantic ('concept') search over the indexed codebase -- finds code by "
                "MEANING, not exact text. Use this when grep_files would need you to "
                "guess exact wording (e.g. 'find where we handle OAuth token refresh' "
                "when the code never uses those exact words) or when you're not sure "
                "which file/function is relevant. Complements, doesn't replace, "
                "grep_files (exact text) and lsp_find_references (exact symbol). "
                "Requires rag_index_directory to have been run first -- will say so "
                "clearly if the index is empty."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A natural-language description of what you're looking for."},
                    "n_results": {"type": "integer", "description": "How many top matches to return (default 5)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_index_stats",
            "description": (
                "Check how many files/chunks are currently in the semantic search index, "
                "so you know whether it's stale or empty before trusting a rag_search result."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

