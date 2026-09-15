#!/usr/bin/env python3
"""
Expt 2 — headless end-to-end smoke test.

Boots the real HTTP server on an ephemeral port, performs the JSON-RPC
handshake, then drives the agent with sample queries — no browser needed.

    python3 smoke_test.py
"""
import json
import threading

import app as appmod
from agent import Agent, ConnectorClient

QUERIES = [
    "What's the weather in Tokyo?",
    "Will it rain in Chennai today?",
    "Compare the weather in London and New York",
    "What's the weather in Xyzzyville123?",   # error-handling path
    "hi",
]


def main():
    srv = appmod.build_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[smoke] server up on 127.0.0.1:{port}")

    client = ConnectorClient(f"http://127.0.0.1:{port}/rpc")
    info, tools = client.handshake()
    print(f"[smoke] handshake OK -> {info['serverInfo']} | tools: {[t['name'] for t in tools]}")

    agent = Agent(client)
    session: dict = {}
    for q in QUERIES:
        print("\n" + "═" * 72)
        print("USER :", q)
        print("─" * 72)
        for ev in agent.handle(q, session):
            t = ev["type"]
            if t == "thought":
                print("🧠  ", ev["text"])
            elif t == "tool_call":
                print("🔧  ", ev["tool"], json.dumps(ev["arguments"]))
            elif t == "tool_result":
                if ev["ok"]:
                    d = ev["data"]
                    cur = d.get("current", {})
                    print(f"📄   OK · {d.get('resolved_place', {}).get('area')} · "
                          f"{cur.get('temperature')}{d.get('units', {}).get('temperature')} "
                          f"{cur.get('condition')} · source={d.get('source')}")
                else:
                    print("📄   ERROR ·", ev["data"])
            elif t == "answer":
                print("💬\n" + ev["text"])
            elif t == "error":
                print("⚠️  ", ev["text"])
    srv.shutdown()
    print("\n[smoke] all queries done ✅")


if __name__ == "__main__":
    main()
