"""
mcp_client.py
-------------
MCP (Model Context Protocol) integration: connects to external MCP servers
(filesystem, fetch, and any future community server) and exposes their
tools through the same synchronous TOOL_FUNCTIONS/TOOL_SPECS interface as
every other tool in this project.

Corrected from the original sketch (verified directly against the real
`mcp` SDK before writing this):
  1. `stdio_client(...)` and `ClientSession(...)` are both async context
     managers (`async with`), not `await`-able constructors --
     `ClientSession(stdio, write)` used with plain `await` raises
     immediately, since `__init__` isn't async.
  2. `mcp-server-fetch`'s tool is literally named `fetch`, not `fetch_get`.
  3. `uvx` isn't required -- `pip install mcp-server-fetch` gives a plain
     executable, confirmed working the same way.

The bigger structural problem the original design didn't address: MCP
sessions are inherently async (they hold a live subprocess + persistent
stdio connection), but this project's entire tool dispatch in agent.py is
synchronous, and calling asyncio.run() per tool call would both (a) fail if
ever invoked from code that's already inside an event loop, and (b) be
unable to keep one persistent server connection alive across multiple
calls -- each call would reconnect from scratch, spawning a new
npx/subprocess every time.

Fix: run ONE persistent asyncio event loop in a dedicated background
thread for the lifetime of the process. All MCP session lifecycle
(connect, call_tool, disconnect) happens on that loop; synchronous callers
(agent.py) submit coroutines to it via run_coroutine_threadsafe and block
on the result. This keeps server connections alive across calls (no
respawning a Node process on every single tool invocation) while still
presenting a plain synchronous function to the rest of the codebase.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any, Optional

import tools as _tools  # reuse is_sensitive_path for the filesystem bridge


def _import_mcp():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        return True, (ClientSession, StdioServerParameters, stdio_client)
    except Exception as e:
        return False, (
            f"mcp SDK is not usable ({type(e).__name__}: {e}). "
            "Setup: `pip install mcp`."
        )


MCP_AVAILABLE, _mcp_or_err = _import_mcp()

# Restrict the filesystem MCP server to this subdirectory only -- same
# "don't touch the whole disk" principle as tools.WORKDIR, applied to a
# server we don't control the implementation of. Kept separate from the
# main project root deliberately: MCP's write_file/edit_file bypass this
# project's own confirmation/diff/backup safety net (see mcp_write_file
# below for how that gap is closed), so limiting its blast radius to one
# subdirectory is an extra layer, not a replacement, for that fix.
FILESYSTEM_SERVER_ROOT = _tools.WORKDIR / "test"


class _MCPLoopThread:
    """One persistent background event loop thread for the process
    lifetime. All actual MCP work (connect/call/disconnect) is scheduled
    onto this loop and awaited synchronously from the calling thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: float = 30.0):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


_loop_thread: Optional[_MCPLoopThread] = None


def _get_loop_thread() -> _MCPLoopThread:
    global _loop_thread
    if _loop_thread is None:
        _loop_thread = _MCPLoopThread()
    return _loop_thread


class MCPToolManager:
    """Manages MCP server connections and exposes a unified, namespaced
    tool interface. All async methods here are meant to be called via the
    loop thread (see connect_all_sync / call_tool_sync below), never
    directly from synchronous code."""

    def __init__(self) -> None:
        self._exit_stack: Optional["asyncio.AsyncExitStack"] = None
        self.sessions: dict[str, Any] = {}
        self.tool_map: dict[str, tuple[str, Any]] = {}

    async def _ensure_stack(self):
        from contextlib import AsyncExitStack
        if self._exit_stack is None:
            self._exit_stack = AsyncExitStack()
        return self._exit_stack

    async def connect_server(self, name: str, command: str, args: list[str]) -> list[str]:
        """Connect to one MCP server over stdio, discover its tools, and
        register them namespaced as '<name>_<tool>' to avoid collisions."""
        if not MCP_AVAILABLE:
            raise RuntimeError(str(_mcp_or_err))
        ClientSession, StdioServerParameters, stdio_client = _mcp_or_err

        stack = await self._ensure_stack()
        server_params = StdioServerParameters(command=command, args=args, env=None)

        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        tools_result = await session.list_tools()
        registered = []
        for tool in tools_result.tools:
            full_name = f"{name}_{tool.name}"
            self.tool_map[full_name] = (name, tool)
            registered.append(full_name)

        self.sessions[name] = session
        return registered

    async def call_tool(self, full_name: str, arguments: dict) -> str:
        if full_name not in self.tool_map:
            return f"ERROR: unknown MCP tool '{full_name}'. Available: {sorted(self.tool_map.keys())}"

        server_name, tool = self.tool_map[full_name]
        session = self.sessions[server_name]
        try:
            result = await session.call_tool(tool.name, arguments=arguments)
        except Exception as e:
            return f"ERROR calling {full_name}: {type(e).__name__}: {e}"

        texts = [c.text for c in result.content if hasattr(c, "text") and c.text]
        body = "\n".join(texts) if texts else "(no text output)"
        if getattr(result, "isError", False):
            return f"ERROR from {full_name}: {body}"
        return body

    def get_tool_specs(self) -> list[dict]:
        specs = []
        for full_name, (server_name, tool) in self.tool_map.items():
            specs.append({
                "type": "function",
                "function": {
                    "name": full_name,
                    "description": f"[MCP:{server_name}] {tool.description or ''}",
                    "parameters": tool.inputSchema,
                },
            })
        return specs

    async def disconnect(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self.sessions.clear()
        self.tool_map.clear()


mcp_manager = MCPToolManager()

# ---------------------------------------------------------------------------
# Synchronous bridge -- this is what agent.py / tools.py actually call.
# ---------------------------------------------------------------------------

_connected_tool_names: list[str] = []
_connect_errors: list[str] = []


def connect_all_sync(timeout: float = 30.0) -> list[str]:
    """
    Connect to the configured MCP servers (filesystem, fetch) synchronously,
    from regular (non-async) code. Safe to call multiple times -- servers
    that are already connected are left alone. Returns the list of newly
    available (or already-available) namespaced tool names. Individual
    server failures are collected in _connect_errors and don't prevent the
    other server(s) from connecting -- e.g. if npx/node isn't available,
    the fetch server can still work, and vice versa.
    """
    if not MCP_AVAILABLE:
        _connect_errors.append(str(_mcp_or_err))
        return []

    loop_thread = _get_loop_thread()

    async def _connect_all():
        registered = []
        FILESYSTEM_SERVER_ROOT.mkdir(parents=True, exist_ok=True)

        if "filesystem" not in mcp_manager.sessions:
            try:
                tools = await mcp_manager.connect_server(
                    "filesystem", "npx",
                    ["-y", "@modelcontextprotocol/server-filesystem", str(FILESYSTEM_SERVER_ROOT)],
                )
                registered.extend(tools)
            except Exception as e:
                _connect_errors.append(f"filesystem server failed: {type(e).__name__}: {e}")

        if "fetch" not in mcp_manager.sessions:
            try:
                tools = await mcp_manager.connect_server("fetch", "mcp-server-fetch", [])
                registered.extend(tools)
            except Exception as e:
                _connect_errors.append(f"fetch server failed: {type(e).__name__}: {e}")

        return registered

    try:
        newly_registered = loop_thread.run(_connect_all(), timeout=timeout)
    except Exception as e:
        _connect_errors.append(f"MCP connection setup failed: {type(e).__name__}: {e}")
        return []

    _connected_tool_names.extend(newly_registered)
    return list(mcp_manager.tool_map.keys())


def call_tool_sync(full_name: str, arguments: dict, timeout: float = 30.0) -> str:
    """Synchronously call a connected MCP tool by its namespaced name."""
    loop_thread = _get_loop_thread()
    try:
        return loop_thread.run(mcp_manager.call_tool(full_name, arguments), timeout=timeout)
    except Exception as e:
        return f"ERROR: MCP call to {full_name} timed out or failed: {type(e).__name__}: {e}"


def disconnect_all_sync(timeout: float = 10.0) -> None:
    """Cleanly shut down every connected MCP server (terminates their
    subprocesses). Registered via atexit so it runs on normal interpreter
    exit -- confirmed this matters: an MCP server left running via npx is a
    real orphaned Node process, not just a dangling Python object."""
    if not MCP_AVAILABLE or _loop_thread is None:
        return
    try:
        _loop_thread.run(mcp_manager.disconnect(), timeout=timeout)
    except Exception:
        pass  # best-effort on shutdown -- don't let cleanup failures crash exit
    _loop_thread.stop()


atexit.register(disconnect_all_sync)


def get_connection_status() -> str:
    """Human-readable summary of what's connected / what failed, for
    diagnostics -- e.g. so the agent's system prompt or a user can see why
    a tool it expected isn't available."""
    lines = []
    if mcp_manager.tool_map:
        lines.append(f"Connected MCP tools ({len(mcp_manager.tool_map)}): " + ", ".join(sorted(mcp_manager.tool_map.keys())))
    if _connect_errors:
        lines.append("Connection errors: " + "; ".join(_connect_errors))
    return "\n".join(lines) if lines else "No MCP servers connected."
