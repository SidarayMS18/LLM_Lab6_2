# Expt 2 — The "Data Dashboard" Connector

**Objective:** Connect an AI to an external data service (a weather API) to fetch
**real-time data** and summarize it — the classic **"context enhancement"** use case.
The LLM doesn't know today's weather; a **tool** fetches it, and the model weaves that
fresh context into its answer.

---

## 1. What was built

A complete, runnable client–server system in **pure Python standard library**
(zero pip dependencies, zero API keys):

| Role | File | What it does |
|---|---|---|
| **Tool server** (server) | `mcp_server.py` | Exposes `get_current_weather(location, units)` over **JSON-RPC 2.0 / HTTP**. Fetches live data from **wttr.in** (free, no key); falls back to **Open-Meteo** if wttr.in is down. Returns the result as **structured text (JSON)**. |
| **Agent client** (client) | `agent.py` | The "LLM decision loop": interprets the user's message → decides to call the tool → sends `tools/call` over the wire → reads the structured JSON → writes the final answer. Every decision is emitted as a **trace event**. Includes `SimulatedLLM` (deterministic, runs with no keys) and `GroqLLM` — a **real LLM doing genuine function-calling** via the Groq API (`openai/gpt-oss-120b` by default). `OpenAILLM` / any OpenAI-compatible endpoint also work. |
| **Host app** | `app.py` | One process serving both roles + the UI: `POST /rpc` (tool server), `POST /api/chat` (agent, streamed as Server-Sent Events), `GET /` (dashboard UI). |
| **Dashboard UI** | `ui/index.html` | Chat window that **shows the LLM's thought process live** — thinking steps, the exact tool call with arguments, the raw JSON result — plus a **JSON-RPC wire log** panel making the client↔server interaction tangible. |
| **Smoke test** | `smoke_test.py` | Headless end-to-end run of 5 queries (incl. the error path); prints the whole trace. |

## 2. Architecture

```
┌───────────────  BROWSER (ui/index.html)  ───────────────┐
│  chat window          reasoning trace        wire log   │
└───────┬─────────────────────┬───────────────────▲──────┘
        │ POST /api/chat      │ POST /rpc         │ SSE trace events
        ▼                     ▼                   │
┌───────────────  APP (app.py, one process)  ─────────────┐
│  AGENT (client)                  TOOL SERVER (/rpc)     │
│  agent.py                        mcp_server.py          │
│  ┌───────────────────┐   JSON-RPC 2.0   ┌─────────────┐ │
│  │ LLM decision loop │ ───────────────► │ tools/list  │ │
│  │  1. interpret     │   tools/call     │ tools/call  │ │
│  │  2. call tool     │ ◄─────────────── │ get_current_│ │
│  │  3. observe JSON  │  structured text │   weather() │ │
│  │  4. summarise     │                  └──────┬──────┘ │
│  └───────────────────┘                         │        │
└─────────────────────────────────────────────────┼────────┘
                                                  ▼
                                 wttr.in  (fallback: open-meteo.com)
```

The agent reaches the tool server through a **real HTTP hop** (`http://127.0.0.1:<port>/rpc`)
— client and server never share objects — exactly like a remote MCP/tool server.

## 3. How to run

```bash
cd expt2-data-dashboard
python3 app.py                 # → http://localhost:8000
```

Open the URL, then type: **“What’s the weather in Tokyo?”**

Headless demo / grading without a browser:

```bash
python3 smoke_test.py
```

Optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | HTTP port |
| `GROQ_API_KEY` | – | Swaps the rule-based brain for a **real LLM** doing genuine function-calling via **Groq** (free key at console.groq.com). Also accepted: a `groq_api_key.txt` file next to the code |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model (tool-calling capable; `openai/gpt-oss-20b` = faster, `groq/compound-mini` = agentic) |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1/chat/completions` | Any OpenAI-compatible endpoint instead of Groq |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | – | Alternative real brain if you'd rather use OpenAI |
| `WEATHER_OFFLINE` | off | `1` → serve clearly-marked simulated data (fully offline demo) |
| `WEATHER_CACHE_TTL` | `600` | Seconds to cache upstream responses |
| `CHAT_PACING` | `0.45` | Pause between trace events (dramatic effect) |

> **Which brain am I running?** `GET /api/health` reports it, and the header chip in
> the UI shows it live: *Groq · openai/gpt-oss-120b* vs *SimulatedLLM*.

## 4. The tool contract (what the LLM plans against)

`POST /rpc` with `{"method": "tools/list"}` returns the machine-readable catalog:

```json
{
  "name": "get_current_weather",
  "description": "Fetch LIVE current weather for a city, plus a compact 3-day outlook…",
  "inputSchema": {
    "type": "object",
    "properties": {
      "location": {"type": "string", "description": "City name, e.g. 'Tokyo'"},
      "units":    {"type": "string", "enum": ["metric", "imperial"]}
    },
    "required": ["location"]
  }
}
```

`tools/call` returns **structured text** (JSON inside an MCP-style content block):
current conditions (temp, feels-like, humidity, wind, precipitation), today's
min/max + rain chance + sunrise/sunset, and a 3-day forecast — with
`resolved_place` showing exactly which place the data source matched.

## 5. One request, end to end (observed sample run)

User: **“What’s the weather in Tokyo?”** — the actual trace from `smoke_test.py`:

```
🧠   I read this as a live-weather request for “Tokyo”. Checking my tool catalog:
     `get_current_weather(location, units)` matches perfectly — I'll call it once and
     wait for the structured JSON result.
🔧   get_current_weather {"location": "Tokyo", "units": "metric"}
     … JSON-RPC tools/call → server → wttr.in …
📄   OK · Tokyo · 20.0°C Light rain · source=wttr.in
🧠   The tool answered with structured JSON — exactly the 'context enhancement' I
     needed. Key fields: 20°C and light rain, humidity 89%, today's max rain chance
     84%. No further tool calls required — time to write the summary myself.
💬   🌧️ **Tokyo, Japan**
     Right now: **20°C**, *light rain* (feels like 21°C).
     - humidity 89%; wind NE at 7 kph; 0.9 mm precipitation so far.
     - Rain chance today: **~84%** → definitely pack an umbrella ☔.
     - Today: 19°C → 20°C · sun 05:21 AM–05:55 PM.
     - Then: **Sat** 20°C/22°C, light rain shower (rain ~50%) · **Sun** 21°C/27°C, cloudy (rain ~37%).
     _Live data via wttr.in, observed 06:45 AM local_
```

The **agent loop** visible here is the whole point of the experiment:
**interpret → plan → act (tool) → observe (structured JSON) → summarise.**

## 6. Things to try in the UI

| Prompt | What it demonstrates |
|---|---|
| `What's the weather in Tokyo?` | The base case: one tool call, JSON → summary |
| `Will it rain in Chennai today?` | Intent + argument extraction; umbrella advice |
| `Compare London and New York` | The loop iterates: **two** tool calls, then a verdict |
| `Tokyo vs Chennai in fahrenheit` | Second argument (`units=imperial`) extracted from phrasing |
| `What's the weather` | No location → the agent **asks a clarifying question** instead of guessing; then answer just `Chennai` |
| `What's the weather in Xyzzyville123?` | Error handling: the tool returns `isError`, the agent reports honestly and never invents data |
| `hi` / `What can you do?` | The agent decides **no tool call** is needed |

While these run, watch the right-hand panel: `initialize → tools/list` on page load,
then `tools/call` frames with the raw request/response JSON — the client-server
conversation made tangible.

## 7. Design notes (for the report/viva)

- **Why JSON-RPC 2.0?** It's the same transport-agnostic envelope MCP uses
  (`initialize`, `tools/list`, `tools/call` are the exact MCP method names), so the
  architecture maps 1:1 onto real Model-Context-Protocol servers.
- **Context enhancement, precisely:** the model's context window gets *augmented*
  with a small, structured, on-demand payload instead of a giant pre-crawled corpus.
  Structured JSON (not prose) is returned so the model can *reason* over fields.
- **Location resolution:** wttr.in's own geo-matching is quirky for bare city names
  (e.g. "Tokyo" once matched a weather station on Shikinejima island), so the server
  canonically geocodes the place first (Open-Meteo geocoder, free) and reports both
  the canonical place and what wttr matched (`wttr_matched_area`).
- **Resilience:** transient HTTP 429/5xx retried; wttr.in down → automatic Open-Meteo
  fallback; everything fails → an `isError` tool result the LLM must handle honestly.
- **Caching** (10-min TTL) keeps the demo polite to the free APIs.
- **The simulated brain** exists so the lab is reproducible and key-free; the tool
  protocol is identical, so flipping to a real LLM (`OPENAI_API_KEY`) changes only
  the "brain", not the plumbing.

## 8. Extensions

1. Add a second tool (e.g. `get_air_quality` via Open-Meteo's air-quality API) and
   watch the agent *choose between* tools.
2. Swap `GroqLLM`'s endpoint (`GROQ_BASE_URL`) for any OpenAI-compatible server —
   vLLM, LM Studio, Together, OpenAI — the function-calling loop is unchanged.
3. Replace the JSON-RPC shim with the real `mcp` Python SDK (`FastMCP`) — the tool
   function body stays the same.
4. Multi-step planning: "Should I bike to work tomorrow?" → forecast + rain chance +
   a decision rule.

## 9. Viva questions

1. **Why can't the LLM answer weather questions directly?** Its knowledge is frozen at
   training time; live data must be injected into its context at run time — by a tool.
2. **Who decides to call the tool?** The *model* does, by reading the tool catalog
   (name + description + JSON-Schema) and emitting a structured call. The *app*
   executes it. (In this lab the rule-based brain makes that decision deterministically.)
3. **What stops the model from guessing a location?** Nothing structural — that's why
   the agent asks a clarifying question when no location is present, and why
   `isError` results must be surfaced honestly.
4. **Why return JSON text instead of prose?** Fields are unambiguous and compact;
   the summarisation step stays with the model, the fetching stays with the server.
5. **What happens on the wire for one weather query?** `tools/call {location, units}`
   → one HTTP request to wttr.in → `result.content[0].text` = JSON → SSE trace to
   the browser.
