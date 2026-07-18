"""
ws_bridge.py
------------
Thin WebSocket-to-stdio bridge.

Listens on ws://localhost:8765, spawns bridge_server.py as a child process,
and proxies messages between the WebSocket client and the subprocess.
  WS client sends:     "hello world"                       (plain text, backward compat)
                       {"type":"user_message","message":"...","context":"..."}  (new JSON payload)

  WS client receives:  {"type":"log",...}       (newline-delimited JSON from bridge_server.py)

Wire format mapping:
  WS text (plain)  ->  {"type":"run","id":"<uuid>","input":"<text>"}\\n         (-> bridge stdin)
  WS text (JSON)   ->  unpacked, context appended, then same run protocol
  bridge stdout (lines)  ->  WS text messages  (-> client)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid

try:
    import websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)

BRIDGE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_server.py")
HOST = "127.0.0.1"
PORT = 8765


async def _reader(proc: subprocess.Popen, ws) -> None:
    """Read bridge_server.py stdout and forward each line to the WS client."""
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                try:
                    await ws.send(text)
                except websockets.exceptions.ConnectionClosed:
                    break
    except Exception:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


async def handler(ws) -> None:
    """Handle one WebSocket connection: spawn bridge_server.py, proxy messages."""
    print(f"[ws_bridge] client connected from {ws.remote_address}")

    proc = subprocess.Popen(
        [sys.executable, "-u", BRIDGE_SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    # Start reading stdout in background
    reader_task = asyncio.ensure_future(_reader(proc, ws))

    try:
        async for raw in ws:
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text:
                continue

            # Accept either a plain string OR a JSON payload from the
            # extension.  The new extension sends:
            #   {"type":"user_message","message":"...","context":"..."}
            # Plain strings are still handled for backward compat.
            user_message = text
            extra_context = None
            if text.startswith("{"):
                try:
                    payload = json.loads(text)
                    if isinstance(payload, dict) and payload.get("type") == "user_message":
                        user_message = payload.get("message", text)
                        extra_context = payload.get("context")
                except json.JSONDecodeError:
                    pass  # not JSON, treat as plain text

            # Build the run payload for bridge_server.py
            request_id = str(uuid.uuid4())
            bridge_input = user_message
            if extra_context:
                bridge_input = f"{user_message}\n\n[Editor Context]\n{extra_context}"

            msg = json.dumps({"type": "run", "id": request_id, "input": bridge_input})

            try:
                proc.stdin.write((msg + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                try:
                    await ws.send(json.dumps({
                        "type": "error",
                        "id": request_id,
                        "message": "bridge subprocess died"
                    }))
                except Exception:
                    pass
                break
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        reader_task.cancel()
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        print(f"[ws_bridge] client disconnected")


async def main():
    print(f"[ws_bridge] listening on ws://{HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
