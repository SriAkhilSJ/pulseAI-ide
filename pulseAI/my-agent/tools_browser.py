"""
tools_browser.py
-----------------
Browser-based tools (screenshot, local static-site preview, JS evaluation)
built on Playwright + headless Chromium, so the agent can visually verify
what it builds instead of just trusting that HTML/CSS "should" render fine.

Setup (beyond `pip install -r requirements.txt`):
    playwright install chromium
    sudo playwright install-deps chromium   # OS-level shared libs Chromium needs

Confirmed by direct testing in this sandbox: `pip install playwright` alone
is NOT enough -- headless Chromium failed to launch with
"error while loading shared libraries: libnspr4.so: cannot open shared
object file", fixed only after `playwright install-deps chromium` (which
needs sudo/root to apt-install system packages). If those OS deps aren't
installed on whatever machine this runs on, these tools fail gracefully
(see _import_playwright below) rather than crashing the whole agent.

Design notes:
- Every call here is self-contained: launches Playwright, does one thing,
  closes it. No shared/global browser instance kept alive across calls.
  Simpler and safer than a persistent singleton (matches the "keep it
  boring" philosophy in tools.py), at the cost of ~1s startup per call.
- Local files are only ever served via a real local HTTP server bound to
  127.0.0.1 on an OS-assigned free port -- never opened directly via a
  file:// URL. This matters in practice: sites that use fetch()/XHR for
  relative assets (a very common pattern) behave differently or fail
  under file://, so testing through an actual HTTP server is a much more
  honest simulation of how the page will really be served/viewed.
- Paths are resolved through tools._resolve so these tools inherit the
  same "stay inside the project directory" restriction as read_file/
  write_file, and screenshot output paths go through the same sensitive-
  path check (defense in depth, even though a .png is unlikely to matter).
- Console errors and page errors during load are captured and reported
  back as part of the tool's result, directly addressing "no errors in
  browser console" as a checkable success criterion rather than something
  the agent has to take on faith.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from pathlib import Path
from typing import Optional

import tools as _tools  # reuse _resolve / is_sensitive_path / WORKDIR

MAX_CONSOLE_MESSAGES = 30
PAGE_LOAD_TIMEOUT_MS = 15000


def _import_playwright():
    """Import Playwright lazily and return (ok, sync_playwright_or_error_message).
    Lets tools.py decide whether to expose these tools at all, and lets a
    single call fail with a clear, actionable message instead of an import
    crash if Playwright or its browser/OS deps aren't installed."""
    try:
        from playwright.sync_api import sync_playwright
        return True, sync_playwright
    except Exception as e:
        return False, (
            "Playwright is not usable in this environment "
            f"({type(e).__name__}: {e}). Setup: `pip install playwright`, then "
            "`playwright install chromium`, then (Linux) "
            "`sudo playwright install-deps chromium` for the OS-level shared "
            "libraries headless Chromium needs."
        )


def _free_port() -> int:
    """Ask the OS for a free TCP port instead of guessing/hardcoding one,
    to avoid collisions if something else is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    """Poll until something is actually listening on `port`, instead of a
    fixed sleep -- more robust and usually faster."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Same as SimpleHTTPRequestHandler but doesn't spam stdout/stderr with
    a log line per request -- we only care about the screenshot result."""
    def log_message(self, format, *args):
        pass


def _serve_directory(directory: Path, port: int) -> threading.Thread:
    """Start a real local HTTP server rooted at `directory` on `port`, in a
    background thread, and return that thread so the caller can decide when
    to stop it (daemon=True means it won't block process exit either way)."""
    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(directory), **kwargs)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.httpd = httpd  # stash a reference so callers can call .shutdown()
    thread.start()
    return thread


def _capture_console(page) -> list[str]:
    """Wire up console/page-error listeners on `page`, returning the list
    they'll be appended to (call this BEFORE navigating)."""
    messages: list[str] = []

    def on_console(msg):
        if len(messages) < MAX_CONSOLE_MESSAGES and msg.type in ("error", "warning"):
            messages.append(f"[console.{msg.type}] {msg.text}")

    def on_pageerror(exc):
        if len(messages) < MAX_CONSOLE_MESSAGES:
            messages.append(f"[uncaught exception] {exc}")

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    return messages


def _format_console_report(messages: list[str]) -> str:
    if not messages:
        return "No console errors or warnings detected."
    return "Console issues detected:\n" + "\n".join(f"  - {m}" for m in messages)


def screenshot_url(
    url: str,
    output_path: str = "screenshot.png",
    full_page: bool = True,
    width: int = 1280,
    height: int = 800,
) -> str:
    """Take a screenshot of a live http(s) URL and save it as a PNG inside
    the project directory. Reports any console errors/warnings seen during
    load. `width`/`height` set the browser viewport (e.g. 375 for a mobile
    check) -- see test_local_html's docstring for why this matters for
    genuine responsive-layout verification."""
    ok, sync_playwright_or_err = _import_playwright()
    if not ok:
        return f"ERROR: {sync_playwright_or_err}"

    try:
        out = _tools._resolve(output_path)
        if _tools.is_sensitive_path(str(out)):
            return f"ERROR: refusing to write screenshot to sensitive path '{output_path}'."
    except Exception as e:
        return f"ERROR: invalid output_path: {e}"

    width = max(200, min(int(width), 4000))
    height = max(200, min(int(height), 4000))

    sync_playwright = sync_playwright_or_err
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                console_log = _capture_console(page)
                page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="load")
                out.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out), full_page=full_page)
            finally:
                browser.close()
    except Exception as e:
        return f"ERROR: failed to screenshot {url}: {type(e).__name__}: {e}"

    size = out.stat().st_size if out.exists() else 0
    return (
        f"OK: screenshot of {url} saved to {output_path} ({size} bytes).\n"
        f"{_format_console_report(console_log)}"
    )


def test_local_html(
    file_path: str,
    output_path: Optional[str] = None,
    full_page: bool = True,
    width: int = 1280,
    height: int = 800,
) -> str:
    """
    Serve the directory containing `file_path` over a real local HTTP
    server (not a file:// URL -- see module docstring for why), navigate to
    it in headless Chromium, screenshot the rendered page, and report any
    console errors/warnings. This is the tool to use to visually verify a
    static site actually renders (not just that write_file succeeded).

    `width`/`height` set the actual browser viewport -- pass e.g. width=375
    to genuinely render at a mobile breakpoint. IMPORTANT: found directly
    that without this parameter, "verify at mobile width" claims were being
    made by reasoning about CSS media queries without ever actually
    rendering at that width -- always pass the real target width rather
    than assuming responsive CSS behaves as intended.
    """
    ok, sync_playwright_or_err = _import_playwright()
    if not ok:
        return f"ERROR: {sync_playwright_or_err}"

    try:
        if _tools.is_sensitive_path(file_path):
            return f"ERROR: refusing to preview sensitive path '{file_path}'."
        target = _tools._resolve(file_path)
        if not target.exists() or not target.is_file():
            return f"ERROR: file not found: {file_path}"
    except Exception as e:
        return f"ERROR: invalid file_path: {e}"

    width = max(200, min(int(width), 4000))
    height = max(200, min(int(height), 4000))

    output_path = output_path or (target.stem + "_screenshot.png")
    try:
        out = _tools._resolve(output_path)
    except Exception as e:
        return f"ERROR: invalid output_path: {e}"

    port = _free_port()
    server_thread = _serve_directory(target.parent, port)
    try:
        if not _wait_for_port(port):
            return "ERROR: local preview server did not start in time."

        sync_playwright = sync_playwright_or_err
        url = f"http://127.0.0.1:{port}/{target.name}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": width, "height": height})
                    console_log = _capture_console(page)
                    response = page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="load")
                    status = response.status if response else None
                    out.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(out), full_page=full_page)
                    # Report the ACTUAL rendered scroll width vs viewport width,
                    # so "no horizontal scroll" is a checkable fact from this
                    # tool's result, not something inferred separately.
                    scroll_width = page.evaluate("document.documentElement.scrollWidth")
                finally:
                    browser.close()
        except Exception as e:
            return f"ERROR: failed to render {file_path}: {type(e).__name__}: {e}"
    finally:
        server_thread.httpd.shutdown()

    size = out.stat().st_size if out.exists() else 0
    status_note = f"HTTP status: {status}. " if status is not None else ""
    overflow_note = ""
    if scroll_width and scroll_width > width:
        overflow_note = (
            f"\nWARNING: page content is {scroll_width}px wide but the viewport "
            f"was only {width}px -- this means there IS horizontal scroll/overflow "
            f"at this width, which usually indicates a responsive-layout bug."
        )
    return (
        f"OK: rendered {file_path} at {width}x{height} viewport via local server "
        f"({status_note}served from http://127.0.0.1:{port}/), screenshot saved to "
        f"{output_path} ({size} bytes).\n"
        f"{_format_console_report(console_log)}"
        f"{overflow_note}"
    )



def evaluate_js(file_path: str, script: str) -> str:
    """
    Load `file_path` (served via a real local HTTP server, same as
    test_local_html) and run `script` as JavaScript in the page context,
    returning its result. Use this for things like checking
    document.querySelectorAll(...).length, or confirming an element/class
    exists, without needing a screenshot.
    """
    ok, sync_playwright_or_err = _import_playwright()
    if not ok:
        return f"ERROR: {sync_playwright_or_err}"

    try:
        if _tools.is_sensitive_path(file_path):
            return f"ERROR: refusing to evaluate against sensitive path '{file_path}'."
        target = _tools._resolve(file_path)
        if not target.exists() or not target.is_file():
            return f"ERROR: file not found: {file_path}"
    except Exception as e:
        return f"ERROR: invalid file_path: {e}"

    port = _free_port()
    server_thread = _serve_directory(target.parent, port)
    try:
        if not _wait_for_port(port):
            return "ERROR: local preview server did not start in time."

        sync_playwright = sync_playwright_or_err
        url = f"http://127.0.0.1:{port}/{target.name}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    console_log = _capture_console(page)
                    page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="load")
                    result = page.evaluate(script)
                finally:
                    browser.close()
        except Exception as e:
            return f"ERROR: failed to evaluate script against {file_path}: {type(e).__name__}: {e}"
    finally:
        server_thread.httpd.shutdown()

    return f"Result: {result!r}\n{_format_console_report(console_log)}"


def get_accessibility_snapshot(
    file_path: Optional[str] = None,
    url: Optional[str] = None,
    depth: int = 8,
) -> str:
    """
    Return a structured accessibility snapshot (ARIA roles/names/states) of
    a page -- either a local file (served via the same real local HTTP
    server as test_local_html/evaluate_js, never file://) or a live http(s)
    URL. Gives the agent DOM semantics ("heading", "button", "textbox 
    Search") instead of having to infer structure from a screenshot or
    guess selectors from raw HTML source.

    Exactly one of file_path/url must be given.

    API note: this uses `locator.aria_snapshot(depth=...)`, NOT
    `page.accessibility.snapshot()`. A prior proposed implementation used
    the latter -- confirmed directly, by calling it against the real
    installed Playwright (1.61.0), that `page.accessibility` does not exist
    at all anymore (`AttributeError: 'Page' object has no attribute
    'accessibility'`) -- that whole API was removed years ago. The current,
    real replacement is `Locator.aria_snapshot()`, which also returns an
    already-formatted, readable YAML-like string (headings, roles, nesting)
    rather than a raw nested dict that has to be manually walked and
    formatted -- so this implementation is simpler than the original
    proposal's manual recursive tree-printer, not just corrected.
    """
    if bool(file_path) == bool(url):
        return "ERROR: pass exactly one of file_path or url, not both/neither."

    ok, sync_playwright_or_err = _import_playwright()
    if not ok:
        return f"ERROR: {sync_playwright_or_err}"

    depth = max(1, min(int(depth), 20))
    sync_playwright = sync_playwright_or_err

    if url:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    console_log = _capture_console(page)
                    page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="load")
                    snapshot = page.locator("body").aria_snapshot(depth=depth)
                finally:
                    browser.close()
        except Exception as e:
            return f"ERROR: failed to get accessibility snapshot of {url}: {type(e).__name__}: {e}"

        snapshot = _tools._truncate(
            snapshot, _tools.MAX_TOOL_OUTPUT_CHARS,
            "Pass a smaller depth to get a more compact snapshot.",
        )
        return f"Accessibility snapshot of {url}:\n{snapshot}\n{_format_console_report(console_log)}"

    # Local file path -- same HTTP-server pattern as test_local_html/evaluate_js.
    try:
        if _tools.is_sensitive_path(file_path):
            return f"ERROR: refusing to inspect sensitive path '{file_path}'."
        target = _tools._resolve(file_path)
        if not target.exists() or not target.is_file():
            return f"ERROR: file not found: {file_path}"
    except Exception as e:
        return f"ERROR: invalid file_path: {e}"

    port = _free_port()
    server_thread = _serve_directory(target.parent, port)
    try:
        if not _wait_for_port(port):
            return "ERROR: local preview server did not start in time."

        page_url = f"http://127.0.0.1:{port}/{target.name}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    console_log = _capture_console(page)
                    page.goto(page_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="load")
                    snapshot = page.locator("body").aria_snapshot(depth=depth)
                finally:
                    browser.close()
        except Exception as e:
            return f"ERROR: failed to get accessibility snapshot of {file_path}: {type(e).__name__}: {e}"
    finally:
        server_thread.httpd.shutdown()

    snapshot = _tools._truncate(
        snapshot, _tools.MAX_TOOL_OUTPUT_CHARS,
        "Pass a smaller depth to get a more compact snapshot.",
    )
    return f"Accessibility snapshot of {file_path}:\n{snapshot}\n{_format_console_report(console_log)}"


# Availability flag + tool registration, checked once at import time so
# tools.py can decide whether to expose these at all -- an environment
# without Playwright/Chromium/OS-deps simply won't offer these tools to the
# LLM, rather than offering them and having every call fail.
BROWSER_TOOLS_AVAILABLE, _availability_detail = _import_playwright()

BROWSER_TOOL_FUNCTIONS = {
    "screenshot_url": screenshot_url,
    "test_local_html": test_local_html,
    "evaluate_js": evaluate_js,
    "get_accessibility_snapshot": get_accessibility_snapshot,
}

BROWSER_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "screenshot_url",
            "description": (
                "Take a screenshot of a live http(s) URL using headless Chromium, "
                "saving it as a PNG inside the project. Reports any browser console "
                "errors/warnings seen during load. Use this (not test_local_html) "
                "for URLs served by a real running server (e.g. a Flask app you "
                "started), as opposed to a static local HTML file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to screenshot."},
                    "output_path": {
                        "type": "string",
                        "description": "Where to save the PNG, relative to the project root. Defaults to 'screenshot.png'.",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page, not just the visible viewport. Defaults to true.",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Browser viewport width in pixels (200-4000). Use e.g. 375 for a mobile check. Defaults to 1280.",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Browser viewport height in pixels (200-4000). Defaults to 800.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_local_html",
            "description": (
                "Visually verify a local HTML file by serving it over a real local "
                "HTTP server and screenshotting it with headless Chromium -- use this "
                "to confirm a page actually renders correctly (layout, CSS applied, "
                "no broken content) instead of just assuming write_file succeeding "
                "means it looks right. Reports any console errors/warnings, and "
                "warns explicitly if the page overflows the requested viewport width "
                "(i.e. would have horizontal scroll). To verify a specific responsive "
                "breakpoint (e.g. mobile), pass the real width/height -- reasoning "
                "about CSS media queries without actually rendering at that width is "
                "not verification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the HTML file to preview, relative to the project root.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Where to save the screenshot PNG. Defaults to '<filename>_screenshot.png'.",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page, not just the visible viewport. Defaults to true.",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Browser viewport width in pixels (200-4000). Use e.g. 375 for a mobile check. Defaults to 1280.",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Browser viewport height in pixels (200-4000). Defaults to 800.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_js",
            "description": (
                "Load a local HTML file (via a real local HTTP server) and run a "
                "JavaScript expression against the rendered page, returning its "
                "result -- e.g. checking document.querySelectorAll('.card').length, "
                "or whether a specific element/class is present."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the HTML file to load, relative to the project root.",
                    },
                    "script": {
                        "type": "string",
                        "description": "JavaScript expression to evaluate in the page context.",
                    },
                },
                "required": ["file_path", "script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_accessibility_snapshot",
            "description": (
                "Get a structured accessibility snapshot (ARIA roles, names, states -- "
                "e.g. 'heading \"Total Balance\"', 'button \"Menu\"', 'textbox \"Search\"') "
                "of a page. Use this to understand page structure precisely -- to find "
                "interactive elements, confirm a heading/label's exact text, or check "
                "nesting -- instead of guessing from raw HTML or a screenshot. Pass "
                "exactly one of file_path (local, served via a real local HTTP server) "
                "or url (a live http(s) page)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to a local HTML file, relative to the project root. Mutually exclusive with url.",
                    },
                    "url": {
                        "type": "string",
                        "description": "A live http(s) URL. Mutually exclusive with file_path.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "How many levels of nesting to include (1-20, default 8). Smaller = more compact.",
                    },
                },
            },
        },
    },
]
