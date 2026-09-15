#!/usr/bin/env python3
"""
Expt 2 — The "Data Dashboard" Connector
=======================================
Single-process host for BOTH roles, so one command runs the whole lab:

  *  POST /rpc       -> the TOOL SERVER (JSON-RPC 2.0: initialize / tools/list / tools/call)
  *  POST /api/chat  -> the AGENT (client) as a Server-Sent-Events stream of trace events
  *  GET  /          -> the chat dashboard UI (ui/index.html)

Run:   python3 app.py        then open http://localhost:8000
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mcp_server
from agent import Agent, ConnectorClient, GroqLLM, OpenAILLM, SimulatedLLM

ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "ui" / "index.html"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
PACING = float(os.environ.get("CHAT_PACING", "0.45"))  # seconds between trace events

SESSIONS: dict = {}      # session_id -> {"pending_location": bool, ...}
_AGENT = None
_AGENT_PORT = None


def get_agent() -> Agent:
    """Lazily build the agent once the server's real (possibly ephemeral) port is known."""
    global _AGENT
    if _AGENT is None:
        client = ConnectorClient(f"http://127.0.0.1:{_AGENT_PORT}/rpc")
        llm = GroqLLM(client)                       # preferred real brain (GROQ_API_KEY)
        if llm.available:
            print(f"[agent] brain: {llm.name}")
        else:
            llm = OpenAILLM(client)                 # alt real brain (OPENAI_API_KEY)
            if llm.available:
                print(f"[agent] brain: {llm.name}")
            else:
                llm = SimulatedLLM(client)          # key-free fallback
                print("[agent] brain: SimulatedLLM "
                      "(set GROQ_API_KEY or groq_api_key.txt for a real LLM)")
        _AGENT = Agent(client, llm)
    return _AGENT


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # one compact line per request
        print(f"[http] {fmt % args}")

    # ----------------------------- helpers ----------------------------- #
    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------- GET ------------------------------- #
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/ui", "/ui/"):
            try:
                html = UI_FILE.read_bytes()
            except OSError:
                return self._send_json(500, {"error": "ui/index.html is missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
        elif path == "/api/health":
            self._send_json(200, {
                "ok": True,
                "server": mcp_server.SERVER_INFO,
                "protocol": mcp_server.PROTOCOL_VERSION,
                "transport": "JSON-RPC 2.0 over HTTP",
                "llm": get_agent().llm_name,
                "tools": [mcp_server.TOOL_NAME],
                "sources": ["wttr.in", "open-meteo (fallback)"],
            })
        else:
            self._send_json(404, {"error": f"no route {path}"})

    # ------------------------------- POST ------------------------------ #
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""

        # ---- the TOOL SERVER role (what the agent client connects to) ----
        if path == "/rpc":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as e:
                return self._send_json(200, mcp_server._err(None, -32700, f"Parse error: {e}"))
            result = mcp_server.handle_request(payload)
            if result is None:                      # notification -> no response body
                return self._send_json(202, {"accepted": True})
            return self._send_json(200, result)

        # ---- the AGENT (client) role, streamed as SSE trace events ----
        if path == "/api/chat":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            message = str(body.get("message") or "")
            sid = str(body.get("session_id") or "default")
            session = SESSIONS.setdefault(sid, {})

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(ev):
                self.wfile.write(b"data: "
                                 + json.dumps(ev, ensure_ascii=False).encode("utf-8")
                                 + b"\n\n")
                self.wfile.flush()

            try:
                for ev in get_agent().handle(message, session):
                    emit(ev)
                    kind = ev.get("type")
                    time.sleep(PACING if kind in ("status", "thought")
                               else 0.25 if kind == "tool_call" else 0.05)
                emit({"type": "done"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    emit({"type": "error", "text": f"Server error: {e!r}"})
                    emit({"type": "done"})
                except Exception:
                    pass
            return

        self._send_json(404, {"error": f"no route {path}"})


def build_server(host=None, port=None) -> ThreadingHTTPServer:
    global _AGENT_PORT
    srv = ThreadingHTTPServer((HOST if host is None else host,
                               PORT if port is None else port), Handler)
    srv.daemon_threads = True
    _AGENT_PORT = srv.server_address[1]
    return srv


if __name__ == "__main__":
    srv = build_server()
    url = f"http://localhost:{srv.server_address[1]}"
    print("=" * 64)
    print(" Expt 2  \u00b7  The Data Dashboard Connector")
    print(f"   Chat UI       : {url}")
    print(f"   Tool endpoint : {url}/rpc    (JSON-RPC 2.0)")
    print(f"   Health        : {url}/api/health")
    print("=" * 64)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
