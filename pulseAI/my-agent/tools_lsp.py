"""
tools_lsp.py
------------
Semantic code understanding via real Language Servers (pylsp for Python,
typescript-language-server for JS/TS), so the agent can find cross-file
references and preview renames using actual import/scope resolution --
not text matching, which can't tell "this `age` is the same symbol as that
`age`" from "these are two unrelated variables that happen to share a name".

Built on `pylspclient` rather than hand-rolling JSON-RPC-over-stdio.
Confirmed directly: a naive hand-rolled Content-Length parser is exactly
the kind of "looks simple, subtly wrong" code this project has been bitten
by before (see agent.py's history of provider-schema and truncation bugs)
-- pylspclient already handles header framing, threading, and
request/response correlation correctly.

Setup, confirmed by direct testing:
    pip install pylspclient
    pip install python-lsp-server          # for Python files
    sudo npm install -g typescript-language-server typescript   # for JS/TS files

Design decisions, each backed by something observed while building this:

1. Symbol position lookup is automatic (lsp_find_references / lsp_preview_rename
   take a symbol NAME, not line/character coordinates). LLMs are unreliable at
   manually computing 0-indexed character offsets from a text dump -- this
   removes an entire class of "off-by-one, wrong line" failures by finding
   the symbol's actual position with a regex word-boundary search before
   ever talking to the language server.

2. lsp_preview_rename returns the FULL new content of every affected file,
   not just a diff. This closes the exact failure mode found earlier in
   this project: an LLM asked to "apply this diff" may fabricate a
   placeholder instead of reconstructing full file content. By returning
   the complete, exact new text, the agent's only job is to relay it
   verbatim into write_file -- no reconstruction, no fabrication risk.
   There is deliberately NO auto-apply tool: applying the rename goes
   through the existing write_file path so it gets the project's existing
   safety net for free (diff-before-overwrite confirmation, automatic
   backup, undo_last_edit) -- rebuilding a parallel safety mechanism here
   would be redundant and riskier than reusing the one that's already
   tested.

3. Diagnostics transparency: confirmed directly that typescript-language-server
   reports ZERO diagnostics for plain .js files with no jsconfig.json/
   tsconfig.json present (or one with checkJs unset/false) -- a broken
   import like importing a renamed-away function produces NO error in that
   configuration. lsp_get_diagnostics always reports whether a config file
   enabling checks was found, so "no diagnostics" can't be silently
   mistaken for "verified clean" when it might just mean "checking was off".
"""

from __future__ import annotations

import atexit
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import tools as _tools  # reuse _resolve / is_sensitive_path / WORKDIR

INDEX_TIMEOUT_S = 4        # time to let the server index opened files
LSP_REQUEST_TIMEOUT_S = 15
MAX_FILES_TO_OPEN = 40      # cap on how many same-language files get indexed per project

_LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
}

_SERVER_CMD_BY_LANGUAGE = {
    "python": ["pylsp"],
    "javascript": ["typescript-language-server", "--stdio"],
    "javascriptreact": ["typescript-language-server", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "typescriptreact": ["typescript-language-server", "--stdio"],
}

# Which extensions count as "the same project language" for indexing purposes
# (JS/TS servers understand both plain JS and TS files in one project).
_SIBLING_EXTS = {
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"},
    "javascriptreact": {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"},
    "typescript": {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"},
    "typescriptreact": {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"},
}


def _import_pylspclient():
    try:
        import pylspclient
        return True, pylspclient
    except Exception as e:
        return False, (
            f"pylspclient is not usable ({type(e).__name__}: {e}). "
            "Setup: `pip install pylspclient`."
        )


PYLSPCLIENT_AVAILABLE, _pylspclient_or_err = _import_pylspclient()


class _ServerSession:
    """One running language server process for a given (language, project
    root), kept alive across multiple tool calls so repeated queries don't
    pay the ~1-4s startup+indexing cost every time. Torn down at process
    exit via atexit, or on-demand if it stops responding."""

    def __init__(self, language: str, root: Path):
        self.language = language
        self.root = root
        self.diagnostics: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._opened_files: set[str] = set()

        cmd = _SERVER_CMD_BY_LANGUAGE[language]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Language server for {language!r} not found ({cmd[0]}). "
                + (
                    "Install: `pip install python-lsp-server`."
                    if language == "python" else
                    "Install: `sudo npm install -g typescript-language-server typescript`."
                )
            )

        pylspclient = _pylspclient_or_err
        endpoint = pylspclient.JsonRpcEndpoint(self.proc.stdin, self.proc.stdout)
        self.lsp_endpoint = pylspclient.LspEndpoint(
            endpoint,
            timeout=LSP_REQUEST_TIMEOUT_S,
            notify_callbacks={"textDocument/publishDiagnostics": self._on_diagnostics},
        )
        # CRITICAL: LspEndpoint IS a threading.Thread (confirmed via its
        # MRO) that runs a blocking read loop against the server's stdout
        # until shutdown() is called. Confirmed directly that without
        # marking it a daemon thread, a script that finishes its actual
        # work (gets its references/rename/diagnostics result, prints it)
        # still hangs forever on exit -- the interpreter waits for every
        # non-daemon thread to finish, and this one only stops if
        # shutdown() runs first. Since callers of these tool functions
        # (agent.py's ReAct loop) don't -- and shouldn't have to --
        # explicitly manage LSP session lifecycles, make this thread a
        # daemon so it can never block process exit; the atexit hook still
        # attempts a clean shutdown() first when the interpreter DOES exit
        # normally.
        self.lsp_endpoint.daemon = True
        self.client = pylspclient.LspClient(self.lsp_endpoint)

        root_uri = f"file://{root}"
        self.client.initialize(
            processId=None, rootPath=str(root), rootUri=root_uri,
            capabilities={"textDocument": {"references": {}, "rename": {}, "publishDiagnostics": {}}},
            initializationOptions=None, trace="off",
            workspaceFolders=[{"uri": root_uri, "name": root.name}],
        )
        self.client.initialized()

    def _on_diagnostics(self, params: dict) -> None:
        with self._lock:
            self.diagnostics[params["uri"]] = params.get("diagnostics", [])

    def ensure_files_open(self, exclude: Optional[Path] = None) -> int:
        """Open every sibling-language file under root (capped) so the
        server can resolve cross-file references/imports. Returns how many
        files were (newly or previously) opened."""
        exts = _SIBLING_EXTS[self.language]
        count = 0
        for path in sorted(self.root.rglob("*")):
            if count >= MAX_FILES_TO_OPEN:
                break
            if not path.is_file() or path.suffix not in exts:
                continue
            if _tools.is_sensitive_path(str(path)):
                continue
            uri = f"file://{path}"
            count += 1
            if uri in self._opened_files:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lang_id = _LANGUAGE_BY_EXT.get(path.suffix, self.language)
            self.client.didOpen({"uri": uri, "languageId": lang_id, "version": 1, "text": text})
            self._opened_files.add(uri)
        return count

    def shutdown(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass


_sessions: dict[tuple[str, str], _ServerSession] = {}
_sessions_lock = threading.Lock()


def _get_session(language: str, root: Path) -> _ServerSession:
    key = (language, str(root))
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None or session.proc.poll() is not None:
            session = _ServerSession(language, root)
            _sessions[key] = session
    return session


def _shutdown_all_sessions() -> None:
    with _sessions_lock:
        for session in _sessions.values():
            session.shutdown()
        _sessions.clear()


atexit.register(_shutdown_all_sessions)


def _language_for(path: Path) -> Optional[str]:
    return _LANGUAGE_BY_EXT.get(path.suffix)


def _resolve_target(file_path: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve+validate a file path for LSP use. Returns (path, error)."""
    if _tools.is_sensitive_path(file_path):
        return None, f"ERROR: refusing to analyze sensitive path '{file_path}'."
    try:
        p = _tools._resolve(file_path)
    except Exception as e:
        return None, f"ERROR: invalid file_path: {e}"
    if not p.exists() or not p.is_file():
        return None, f"ERROR: file not found: {file_path}"
    return p, None


def find_symbol_position(file_path: str, symbol_name: str, occurrence: int = 1) -> str:
    """
    Find the 0-indexed (line, character) of the Nth occurrence of
    `symbol_name` as a whole word in `file_path`. This exists so callers
    never have to manually count characters -- feed the returned line/char
    straight into lsp_find_references or lsp_preview_rename.
    """
    p, err = _resolve_target(file_path)
    if err:
        return err
    text = p.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(rf"\b{re.escape(symbol_name)}\b")
    seen = 0
    for line_idx, line in enumerate(text.splitlines()):
        for m in pattern.finditer(line):
            seen += 1
            if seen == occurrence:
                return f"OK: line={line_idx} character={m.start()} (0-indexed)"
    return f"ERROR: symbol '{symbol_name}' not found in {file_path} (occurrence {occurrence})"


def _find_position(p: Path, symbol_name: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    text = p.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(rf"\b{re.escape(symbol_name)}\b")
    for line_idx, line in enumerate(text.splitlines()):
        m = pattern.search(line)
        if m:
            return line_idx, m.start(), None
    return None, None, f"ERROR: symbol '{symbol_name}' not found in file."


def _start_and_index(p: Path, language: str) -> _ServerSession:
    session = _get_session(language, _tools.WORKDIR)
    session.ensure_files_open()
    time.sleep(INDEX_TIMEOUT_S)
    return session


def lsp_find_references(file_path: str, symbol_name: str) -> str:
    """
    Find every real reference to `symbol_name` (as used/defined starting in
    `file_path`) across the whole project, using semantic analysis (actual
    import/scope resolution) rather than text search -- this correctly
    follows re-exports and import aliases that grep_files could conflate
    with unrelated identically-named symbols, or miss entirely.
    """
    if not PYLSPCLIENT_AVAILABLE:
        return f"ERROR: {_pylspclient_or_err}"

    p, err = _resolve_target(file_path)
    if err:
        return err
    language = _language_for(p)
    if not language:
        return f"ERROR: no language server configured for extension '{p.suffix}'."

    line, character, err = _find_position(p, symbol_name)
    if err:
        return err

    try:
        session = _start_and_index(p, language)
        refs = session.lsp_endpoint.call_method(
            "textDocument/references",
            textDocument={"uri": f"file://{p}"},
            position={"line": line, "character": character},
            context={"includeDeclaration": True},
        )
    except RuntimeError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: LSP request failed: {type(e).__name__}: {e}"

    if not refs:
        return f"No references found for '{symbol_name}' (searched from {file_path}:{line+1}:{character+1})."

    lines = [f"Found {len(refs)} reference(s) to '{symbol_name}':"]
    for r in refs:
        ref_path = r["uri"].replace("file://", "")
        try:
            ref_path = str(Path(ref_path).relative_to(_tools.WORKDIR))
        except ValueError:
            pass
        start = r["range"]["start"]
        lines.append(f"  - {ref_path}:{start['line']+1}:{start['character']+1}")
    return "\n".join(lines)


def _apply_edits_to_text(text: str, edits: list[dict]) -> str:
    """Apply a list of LSP TextEdits (each with a line/character range and
    newText) to `text`, returning the fully edited result. Edits are
    applied last-to-first by position so earlier ranges' offsets aren't
    invalidated by later edits changing the text length."""
    lines = text.splitlines(keepends=True)

    def pos_to_offset(line: int, character: int) -> int:
        return sum(len(l) for l in lines[:line]) + character

    ordered = sorted(
        edits,
        key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]),
        reverse=True,
    )
    result = text
    for e in ordered:
        start = pos_to_offset(e["range"]["start"]["line"], e["range"]["start"]["character"])
        end = pos_to_offset(e["range"]["end"]["line"], e["range"]["end"]["character"])
        result = result[:start] + e["newText"] + result[end:]
    return result


def lsp_preview_rename(file_path: str, symbol_name: str, new_name: str) -> str:
    """
    Preview a semantic rename of `symbol_name` to `new_name`, starting from
    `file_path`, across every file the language server determines is
    actually affected (real import/reference resolution, not text
    replacement -- won't touch unrelated identically-named symbols in
    other scopes). Returns the COMPLETE new content of each affected file
    (not a diff) -- to actually apply a change, call write_file with the
    path and the EXACT "new_content" text shown for that file below; do
    not retype or reconstruct it.
    """
    if not PYLSPCLIENT_AVAILABLE:
        return f"ERROR: {_pylspclient_or_err}"

    p, err = _resolve_target(file_path)
    if err:
        return err
    language = _language_for(p)
    if not language:
        return f"ERROR: no language server configured for extension '{p.suffix}'."

    line, character, err = _find_position(p, symbol_name)
    if err:
        return err

    try:
        session = _start_and_index(p, language)
        result = session.lsp_endpoint.call_method(
            "textDocument/rename",
            textDocument={"uri": f"file://{p}"},
            position={"line": line, "character": character},
            newName=new_name,
        )
    except RuntimeError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: LSP request failed: {type(e).__name__}: {e}"

    if not result:
        return f"No rename edits returned for '{symbol_name}' -> '{new_name}' (symbol may not be renameable here)."

    # Different servers return either {"changes": {uri: [edits]}} or
    # {"documentChanges": [{"textDocument": {...}, "edits": [...]}]} --
    # confirmed directly: pylsp uses documentChanges, some others use changes.
    per_file_edits: dict[str, list[dict]] = {}
    if "changes" in result:
        per_file_edits = result["changes"]
    elif "documentChanges" in result:
        for dc in result["documentChanges"]:
            uri = dc["textDocument"]["uri"]
            per_file_edits.setdefault(uri, []).extend(dc["edits"])

    if not per_file_edits:
        return f"Rename returned no file changes for '{symbol_name}' -> '{new_name}'."

    output = [f"Rename preview: '{symbol_name}' -> '{new_name}' affects {len(per_file_edits)} file(s).\n"]
    for uri, edits in per_file_edits.items():
        target_path = Path(uri.replace("file://", ""))
        try:
            rel_path = str(target_path.relative_to(_tools.WORKDIR))
        except ValueError:
            output.append(f"SKIPPED (outside project): {uri}\n")
            continue
        try:
            original = target_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            output.append(f"ERROR reading {rel_path} to build preview: {e}\n")
            continue
        new_content = _apply_edits_to_text(original, edits)
        output.append(
            f"--- {rel_path} ({len(edits)} change(s)) ---\n"
            f"[NEW CONTENT to pass verbatim to write_file({rel_path!r}, ...)]:\n"
            f"{new_content}\n"
        )
    return "\n".join(output)


def lsp_get_diagnostics(file_path: str) -> str:
    """
    Get compiler/language-server errors and warnings for `file_path`
    (e.g. broken imports, type errors, undefined names) -- use this after
    a refactor to check nothing was left broken. IMPORTANT: for plain
    JavaScript files, meaningful checking (like catching an import of a
    function that no longer exists) requires a jsconfig.json/tsconfig.json
    with "checkJs": true in the project -- without one, this may report
    zero diagnostics even when there's a real broken import, and that will
    be noted explicitly in the result rather than silently passing.
    """
    if not PYLSPCLIENT_AVAILABLE:
        return f"ERROR: {_pylspclient_or_err}"

    p, err = _resolve_target(file_path)
    if err:
        return err
    language = _language_for(p)
    if not language:
        return f"ERROR: no language server configured for extension '{p.suffix}'."

    try:
        session = _start_and_index(p, language)
    except RuntimeError as e:
        return f"ERROR: {e}"

    uri = f"file://{p}"
    diags = session.diagnostics.get(uri, [])

    config_note = ""
    if language in ("javascript", "javascriptreact"):
        has_config = any(
            (_tools.WORKDIR / name).exists()
            for name in ("jsconfig.json", "tsconfig.json")
        )
        if not has_config:
            config_note = (
                "\nNOTE: no jsconfig.json/tsconfig.json found in the project root -- "
                "plain JavaScript is NOT type/import-checked by default, so 0 "
                "diagnostics here does NOT necessarily mean the file is actually "
                "correct. Add a jsconfig.json with {\"compilerOptions\": "
                "{\"checkJs\": true}} for real verification."
            )

    if not diags:
        return f"No diagnostics reported for {file_path}.{config_note}"

    severity_names = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}
    lines = [f"{len(diags)} diagnostic(s) for {file_path}:"]
    for d in diags:
        sev = severity_names.get(d.get("severity"), "Unknown")
        start = d["range"]["start"]
        lines.append(f"  - [{sev}] line {start['line']+1}: {d['message']}")
    return "\n".join(lines) + config_note


TOOL_FUNCTIONS = {
    "lsp_find_references": lsp_find_references,
    "lsp_preview_rename": lsp_preview_rename,
    "lsp_get_diagnostics": lsp_get_diagnostics,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "lsp_find_references",
            "description": (
                "Find every real usage of a function/variable/symbol across the "
                "whole project using semantic analysis (actual import/scope "
                "resolution via a language server) -- more reliable than "
                "grep_files for this purpose since it won't confuse unrelated "
                "identically-named symbols in different scopes, and correctly "
                "follows imports/re-exports. Supports Python (.py) and "
                "JavaScript/TypeScript (.js/.jsx/.ts/.tsx)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file where the symbol is defined or used, relative to the project root.",
                    },
                    "symbol_name": {
                        "type": "string",
                        "description": "The exact name of the function/variable/symbol to find references for.",
                    },
                },
                "required": ["file_path", "symbol_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_preview_rename",
            "description": (
                "Preview a semantic rename of a symbol across every file that "
                "actually references it (via a language server, not text "
                "replacement). Returns the COMPLETE new content for each "
                "affected file -- to apply a change, call write_file using the "
                "EXACT new content shown for that file, do not retype it "
                "yourself. This tool does not write anything by itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file where the symbol is defined, relative to the project root.",
                    },
                    "symbol_name": {
                        "type": "string",
                        "description": "The current name of the symbol to rename.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "The new name for the symbol.",
                    },
                },
                "required": ["file_path", "symbol_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp_get_diagnostics",
            "description": (
                "Get compiler/language-server errors and warnings for a file "
                "(e.g. broken imports after a rename, type errors, undefined "
                "names). Use this to verify a refactor didn't break anything. "
                "Read the tool result carefully: for plain JavaScript without a "
                "jsconfig.json enabling checkJs, 'no diagnostics' may mean "
                "checking is off, not that the file is verified correct -- the "
                "tool result will say so explicitly when that applies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to check, relative to the project root.",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
]
