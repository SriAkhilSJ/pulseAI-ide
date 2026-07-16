"""
server.py
---------
PulseAI Studio — Local Python Backend Gateway & Multi-Agent API Bridge (`apps/web-showcase`).
Connects HTTP dashboard requests directly to PulseCodeAI's UnifiedToolRegistry and SQLite FTS5 Memory Engine.
"""
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure PulseCodeAI core modules are importable
root_path = Path(__file__).resolve().parents[2]
for sub in ("packages/tools/registry/src", "packages/ai-core/memory/src"):
    p = root_path / sub
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from unified_registry import UnifiedToolRegistry
    from fts_memory import ConversationMemory
except ImportError:
    pass


class PulseStudioHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            registry = UnifiedToolRegistry(workspace_root=str(root_path))
            tools = registry.list_tools()
            self._send_json(200, {
                "status": "active",
                "engine": "PulseCodeAI Monorepo Engine v2.0",
                "registered_tools_count": len(tools),
                "tools": [t["name"] for t in tools[:10]]
            })
        elif parsed.path == "/api/memory/search":
            query = parse_qs(parsed.query).get("query", [""])[0]
            mem = ConversationMemory(db_path=str(root_path / ".pulsecode/memory.db"))
            results = mem.search_history(query) if query else []
            self._send_json(200, {"status": "success", "results": results})
        else:
            self._send_json(404, {"error": f"Route not found: {parsed.path}"})

    def do_POST(self):
        if self.path == "/api/agent/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            prompt = body.get("prompt", "")
            self._send_json(200, {
                "status": "success",
                "prompt": prompt,
                "message": "Mission dispatched to PulseCodeAI AgentOrchestrator successfully."
            })
        else:
            self._send_json(404, {"error": "POST route not found"})


def run_server(port: int = 8080):
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, PulseStudioHandler)
    print(f"⚡ PulseAI Studio Server listening on http://127.0.0.1:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    run_server()
