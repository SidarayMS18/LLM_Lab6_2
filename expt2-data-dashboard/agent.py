"""
Expt 2 — The "Data Dashboard" Connector
=======================================
AI CLIENT (the "client" role) + the agent loop.

    ConnectorClient : minimal JSON-RPC 2.0 client that talks to the tool server
                      (initialize / tools/list / tools/call — same verbs as MCP).
    Agent           : the "LLM decision loop" — interpret the user message,
                      decide whether a tool call is needed, call it, observe the
                      structured result, then compose a natural-language answer.
                      Every decision is YIELDED as a trace event so the UI can
                      show the thought process live.
    SimulatedLLM    : deterministic, rule-based reasoner -> lab runs with zero
                      API keys, and the reasoning is fully reproducible.
    GroqLLM         : REAL LLM doing genuine function-calling via the Groq API
                      (set GROQ_API_KEY, or drop it in groq_api_key.txt).
                      Groq speaks the OpenAI chat-completions dialect, so the
                      same loop also works with OpenAILLM / any compatible URL.

Trace event types yielded by Agent.handle():
    status / thought / tool_call / tool_result / rpc / answer / error
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

TOOL = "get_current_weather"


def _key(env_var: str, key_file: str) -> str:
    """LLM API key lookup: environment variable first, then a key file
    (groq_api_key.txt / openai_api_key.txt) next to the code."""
    key = (os.environ.get(env_var) or "").strip()
    if key:
        return key
    try:
        return (Path(__file__).resolve().parent / key_file).read_text().strip()
    except OSError:
        return ""

# --------------------------------------------------------------------------- #
# Client: speaks JSON-RPC to the tool server over HTTP (loopback is a real
# network hop — the client and server never share objects).
# --------------------------------------------------------------------------- #
class ConnectorClient:
    def __init__(self, endpoint: str, log=None):
        self.endpoint = endpoint
        self.log = log or (lambda direction, payload: None)
        self._id = 0
        self.server_info = None
        self.tools = []

    def _rpc(self, method: str, params=None, notify: bool = False):
        self._id += 1
        req = {"jsonrpc": "2.0", "method": method}
        if not notify:
            req["id"] = self._id
        if params is not None:
            req["params"] = params
        self.log("out", req)
        data = json.dumps(req).encode("utf-8")
        http = urllib.request.Request(self.endpoint, data=data,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(http, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        if not notify:
            self.log("in", body)
        if not notify and "error" in body:
            raise RuntimeError(f"JSON-RPC error: {body['error']}")
        return None if notify else body.get("result")

    def handshake(self):
        """initialize -> notifications/initialized -> tools/list (MCP order)."""
        self.server_info = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "dashboard-agent", "version": "1.0.0"},
        })
        self._rpc("notifications/initialized", {}, notify=True)
        self.tools = (self._rpc("tools/list") or {}).get("tools", [])
        return self.server_info, self.tools

    def call_tool(self, name: str, arguments: dict):
        res = self._rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        text = "".join(c.get("text", "") for c in res.get("content", [])
                       if c.get("type") == "text")
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = text
        return parsed, bool(res.get("isError"))


# --------------------------------------------------------------------------- #
# NLU helpers for the simulated reasoner
# --------------------------------------------------------------------------- #
WEATHER_RE = re.compile(
    r"\bweather\b|\btemperatures?\b|\btemps?\b|\bforecast\b|\brain(?:ing|ed|s)?\b"
    r"|\bdrizzl\w*\b|\bshowers?\b|\bsnow\w*\b|\bhumid\w*\b|\bumbrella\b|\bsunny\b"
    r"|\bcloudy\b|\bwindy\b|\bmuggy\b|\bclimate\b|\bhow (?:hot|cold|warm)\b"
    r"|\b(?:hot|cold|warm) (?:is it|there|out)\b", re.I)

_GREET_RE = re.compile(
    r"^\s*(?:hi+|hello+|hey+|yo|vanakkam|namaste|hola|good\s*(?:morning|afternoon|evening))"
    r"\b[\s!.?]*$", re.I)

_HELP_RE = re.compile(r"\bhelp\b|\bwhat can you do\b|\bwho are you\b|\bhow do you work\b"
                      r"|\bwhat tools?\b", re.I)

# look-ahead that ends a location capture
_STOP = (r"today\b|tonight\b|tomorrow\b|right now|now\b|currently\b|outside\b|please\b"
         r"|and\b|or\b|vs\.?|versus|with\b|without\b|[?.!,;]|$")
_IN_RE = re.compile(
    r"\b(?:in|at|for)\s+([A-Za-z][A-Za-z0-9 ,.'\u2019\-]{1,60}?)\s*(?=" + _STOP + ")", re.I)
_TRAIL_RE = re.compile(
    r"(?:^|[\s,])([A-Za-z][A-Za-z0-9 ,.'\u2019\-]{1,60}?)\s+(?:weather|temperature|temps"
    r"|forecast|climate)\b", re.I)

_BAD_LOC = {"today", "tonight", "tomorrow", "now", "here", "there", "weather",
            "temperature", "forecast", "climate", "fahrenheit", "celsius",
            "imperial", "metric", "f", "c", "me", "us",
            "the", "a", "an", "what", "whats", "what's", "how", "hows", "how's",
            "is", "it", "in", "at", "for", "of", "that", "this"}

_PREFIX_STRIP = [
    "what's the", "whats the", "what is the", "how's the", "hows the", "how is the",
    "what's", "whats", "how's", "hows", "show me", "tell me", "give me",
    "check", "is", "the", "me",
]


def _clean_loc(s: str):
    s = s.strip(" \t\n.,!?\"'\u201c\u201d")
    s = re.sub(r"['\u2019]s$", "", s)
    s = re.sub(r"^(?:the|a|an)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+(?:weather|temperature|temps|forecast|climate|today|tonight"
               r"|tomorrow|now|currently|please)$", "", s, flags=re.I)
    while True:
        low = s.lower()
        for p in _PREFIX_STRIP:
            if low.startswith(p + " "):
                s = s[len(p) + 1:]
                break
        else:
            break
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.-")
    if not s or len(s) > 64:
        return None
    if s.lower() in _BAD_LOC:
        return None
    return s


def _strip_units_clause(msg: str) -> str:
    return re.sub(r"\s+(?:in|into)\s+(?:fahrenheit|celsius|kelvin|imperial|metric)\b",
                  " ", msg, flags=re.I)


def extract_locations(msg: str):
    msg = _strip_units_clause(msg)
    locs = []
    for m in _IN_RE.finditer(msg):
        loc = _clean_loc(m.group(1))
        if loc:
            locs.append(loc)
    if not locs:
        m = _TRAIL_RE.search(msg)
        if m:
            loc = _clean_loc(m.group(1))
            if loc:
                locs.append(loc)
    # "Tokyo vs Chennai" / "compare London and New York" without prepositions
    if len(locs) < 2:
        parts = re.split(r"\s+(?:vs\.?|versus|and)\s+", msg, flags=re.I)
        if len(parts) == 2 and re.search(r"\b(?:compare|comparison|weather|temperature"
                                         r"|temps|forecast)\b|vs\.?|versus", msg, re.I):
            cands = []
            for part in parts:
                w = re.sub(r"[?!.]", " ", part)
                w = re.sub(r"\b(?:compare|comparison|between|weather|temperature|temps"
                           r"|forecast|climate|today|tonight|tomorrow|now|currently"
                           r"|please|the|what|whats|what's|hows|how|is|it|in|at|for"
                           r"|of|fahrenheit|celsius|imperial|metric)\b", " ", w, flags=re.I)
                c = _clean_loc(w)
                if c:
                    cands.append(c)
            if len(cands) == 2 and cands[0].casefold() != cands[1].casefold():
                return cands
    out = []
    for loc in locs:
        if loc.casefold() not in [x.casefold() for x in out]:
            out.append(loc)
    return out[:3]


def detect_units(msg: str) -> str:
    if re.search(r"fahrenheit|\bimperial\b|degrees?\s*f\b|\u00b0\s?f\b", msg, re.I):
        return "imperial"
    return "metric"


def _looks_like_place(msg: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z ,.'\u2019\-]{1,60}", msg.strip()))


# --------------------------------------------------------------------------- #
# Answer composition (the "summarise the structured result" step)
# --------------------------------------------------------------------------- #
def _fmt(v):
    if v is None:
        return "\u2013"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _emoji(cond: str) -> str:
    c = str(cond).lower()
    if "thunder" in c:
        return "\u26c8\ufe0f"
    if any(k in c for k in ("rain", "drizzle", "shower")):
        return "\U0001f327\ufe0f"
    if any(k in c for k in ("snow", "sleet", "rime", "ice")):
        return "\U0001f328\ufe0f"
    if any(k in c for k in ("fog", "mist", "haze")):
        return "\U0001f32b\ufe0f"
    if "overcast" in c:
        return "\u2601\ufe0f"
    if "cloud" in c:
        return "\u26c5"
    if any(k in c for k in ("clear", "sunny")):
        return "\u2600\ufe0f"
    return "\U0001f324\ufe0f"


def _place_label(d: dict) -> str:
    p = d.get("resolved_place") or {}
    area = p.get("area") or d.get("location_requested") or "unknown place"
    country = p.get("country") or ""
    return f"{area}, {country}" if country else str(area)


def _single_block(r: dict) -> str:
    d = r["data"]
    cur = d.get("current") or {}
    today = d.get("today") or {}
    u = d.get("units") or {}
    sym = u.get("temperature", "\u00b0C")
    cond = str(cur.get("condition", "\u2014"))
    lines = [f"{_emoji(cond)} **{_place_label(d)}**"]

    feels = cur.get("feels_like")
    feels_txt = f" (feels like {_fmt(feels)}{sym})" if feels is not None else ""
    lines.append(f"Right now: **{_fmt(cur.get('temperature'))}{sym}**, "
                 f"*{cond.lower()}*{feels_txt}.")

    det = []
    if cur.get("humidity_pct") is not None:
        det.append(f"humidity {cur.get('humidity_pct')}%")
    if cur.get("wind_speed") is not None:
        det.append(f"wind {cur.get('wind_direction') or ''} at "
                   f"{_fmt(cur.get('wind_speed'))} {u.get('wind', 'kph')}".replace("  ", " "))
    if cur.get("precipitation") is not None:
        det.append(f"{_fmt(cur.get('precipitation'))} {u.get('precipitation', 'mm')} "
                   f"precipitation so far")
    if det:
        lines.append("- " + "; ".join(det) + ".")

    rain = today.get("max_rain_chance_pct")
    if rain is not None:
        if rain < 30:
            advice = "umbrella probably stays home \u2600\ufe0f"
        elif rain < 60:
            advice = "keep an umbrella handy \U0001f302"
        else:
            advice = "definitely pack an umbrella \u2614"
        lines.append(f"- Rain chance today: **~{rain}%** \u2192 {advice}.")

    if today.get("max_temperature") is not None:
        astro = ""
        if today.get("sunrise") and today.get("sunset"):
            astro = f" \u00b7 sun {today['sunrise']}\u2013{today['sunset']}"
        lines.append(f"- Today: {_fmt(today.get('min_temperature'))}{sym} \u2192 "
                     f"{_fmt(today.get('max_temperature'))}{sym}{astro}.")

    nxt = [f for f in (d.get("forecast") or [])[1:] if f.get("date") or f.get("day")]
    if nxt:
        bits = [f"**{f.get('day', '')}** {_fmt(f.get('min_temperature'))}{sym}/"
                f"{_fmt(f.get('max_temperature'))}{sym}, {str(f.get('condition', '')).lower()} "
                f"(rain ~{f.get('max_rain_chance_pct', 0)}%)" for f in nxt]
        lines.append("- Then: " + " \u00b7 ".join(bits) + ".")

    if d.get("simulated"):
        lines.append("_Note: simulated sample data (network unavailable)._")
    else:
        obs = cur.get("observation_time_local")
        tail = f", observed {obs} local" if obs else ""
        lines.append(f"_Live data via {d.get('source', '?')}{tail}_")
    return "\n".join(lines)


def _compare_block(results) -> str:
    lines = ["Here's the side-by-side, straight from the tool results:"]
    temps, rains, sym = {}, {}, "\u00b0"
    for r in results:
        d = r["data"]
        cur = d.get("current") or {}
        today = d.get("today") or {}
        u = d.get("units") or {}
        sym = u.get("temperature", sym)
        label = _place_label(d)
        rain = today.get("max_rain_chance_pct")
        lines.append(f"- **{label}**: {_fmt(cur.get('temperature'))}{sym}, "
                     f"*{str(cur.get('condition', '?')).lower()}*, humidity "
                     f"{_fmt(cur.get('humidity_pct'))}%, rain chance ~{_fmt(rain)}%")
        try:
            temps[label] = float(cur.get("temperature"))
        except (TypeError, ValueError):
            pass
        try:
            rains[label] = float(rain or 0)
        except (TypeError, ValueError):
            pass
    if len(temps) == 2:
        (a, ta), (b, tb) = list(temps.items())
        if abs(ta - tb) >= 1:
            warmer, cooler = (a, b) if ta > tb else (b, a)
            lines.append(f"**Verdict:** {warmer} is ~{abs(ta - tb):.0f}{sym} warmer "
                         f"than {cooler} right now.")
        else:
            lines.append(f"**Verdict:** {a} and {b} are practically tied on temperature.")
        if len(rains) == 2:
            (w1, v1), (w2, v2) = list(rains.items())
            if v1 != v2:
                wetter = w1 if v1 > v2 else w2
                lines.append(f"Rain is more likely in **{wetter}** "
                             f"({_fmt(max(v1, v2))}% vs {_fmt(min(v1, v2))}%).")
    return "\n".join(lines)


def compose_answer(results, want_compare: bool) -> str:
    if want_compare and len(results) >= 2:
        return _compare_block(results)
    return "\n".join(_single_block(r) for r in results[:2])


# --------------------------------------------------------------------------- #
# The built-in (simulated) LLM — a deterministic reasoner whose "thoughts"
# mirror what a real LLM does at each turn of the tool-use loop.
# --------------------------------------------------------------------------- #
class SimulatedLLM:
    name = "SimulatedLLM (rule-based, no API key needed)"

    def __init__(self, client: ConnectorClient):
        self.client = client

    def respond(self, user_msg: str, session: dict):
        msg = (user_msg or "").strip()
        yield {"type": "status", "text": "Reasoning about the request\u2026"}

        if not msg:
            yield {"type": "thought", "text": "Empty input. Nothing to send to a tool."}
            yield {"type": "answer",
                   "text": "Say something like **\u201cWhat's the weather in Tokyo?\u201d**"}
            return

        if _GREET_RE.match(msg):
            yield {"type": "thought", "text": "That's a greeting, not a data request. "
                                              "No tool call is needed \u2014 I can answer directly."}
            yield {"type": "answer",
                   "text": "Hey there! \U0001f44b I'm a tiny agent wired to a **weather tool "
                           "server**. Ask me things like *\u201cWhat's the weather in Tokyo?\u201d*, "
                           "*\u201cWill it rain in Chennai today?\u201d* or "
                           "*\u201cCompare London and New York\u201d*."}
            return

        if _HELP_RE.search(msg):
            yield {"type": "thought",
                   "text": "The user wants my capabilities. My tool catalog has exactly one "
                           f"entry: `{TOOL}(location, units)`. No call needed \u2014 just explain."}
            yield {"type": "answer",
                   "text": "I'm a demo of **tool calling** (a.k.a. function calling):\n"
                           f"- I hold a machine-readable catalog from the server: `{TOOL}`\n"
                           "- When your message matches, I extract the arguments "
                           "(`location`, `units`) and call it over JSON-RPC\n"
                           "- I read the structured JSON it returns and summarise it in "
                           "plain English\n- If info is missing (which city?), I ask a "
                           "follow-up instead of guessing\n\nTry: *weather in Tokyo*, "
                           "*Tokyo vs Chennai in fahrenheit*, or a misspelled place to "
                           "see error handling."}
            return

        units = detect_units(msg)
        pending = bool(session.get("pending_location"))
        is_weather = bool(WEATHER_RE.search(msg))
        locs = extract_locations(msg)

        # follow-up like "Chennai" after I asked "which city?"
        if not locs and pending and _looks_like_place(msg) and len(msg.split()) <= 5:
            locs = [_clean_loc(msg) or msg.strip()]

        if not is_weather and not locs:
            yield {"type": "thought",
                   "text": "Scanning my tool catalog\u2026 nothing in this message matches a "
                           "weather intent and I have no other tools. I'll answer directly, "
                           "without burning a tool call."}
            yield {"type": "answer",
                   "text": "I'm a purpose-built demo agent \u2014 my one superpower is **live "
                           "weather lookups** via the `" + TOOL + "` tool. \U0001f326\ufe0f\n"
                           "Try *\u201cWhat's the weather in Tokyo?\u201d*"}
            return

        if not locs:
            session["pending_location"] = True
            yield {"type": "thought",
                   "text": "Clear weather intent, but no location was mentioned. Calling the "
                           "tool with a guessed city would be wrong \u2014 the right move is a "
                           "short clarifying question. No tool call yet."}
            yield {"type": "answer",
                   "text": "Sure \u2014 **which city** should I look up? "
                           "(e.g., *Tokyo*, *Chennai, India*, *New York*)"}
            return

        session["pending_location"] = False
        plural = len(locs) > 1

        if plural:
            plan = (f"The user wants a comparison of live weather for several places: "
                    f"{', '.join(locs)}.")
        else:
            plan = f"I read this as a live-weather request for \u201c{locs[0]}\u201d."
        extra = f" Requested units: {units}." if units == "imperial" else ""
        yield {"type": "thought",
               "text": plan + extra + " Checking my tool catalog: "
                       f"`{TOOL}(location, units)` matches perfectly \u2014 I'll call it "
                       + (f"{len(locs)} times, once per place, and wait for structured JSON."
                          if plural else "once and wait for the structured JSON result.")}

        results = []
        for i, loc in enumerate(locs):
            args = {"location": loc, "units": units}
            yield {"type": "tool_call", "tool": TOOL, "arguments": args}
            frames, data, is_err = self._call(args)
            for fr in frames:
                yield {"type": "rpc", **fr}
            yield {"type": "tool_result", "tool": TOOL, "ok": not is_err, "data": data}
            results.append({"location": loc, "data": data if not is_err else None,
                            "error": data if is_err else None})
            if plural and i < len(locs) - 1:
                yield {"type": "thought",
                       "text": f"First result ({loc}) is in. {len(locs) - i - 1} more to fetch "
                               "for the comparison \u2014 continuing the loop."}

        ok = [r for r in results if r["data"]]
        if not ok:
            first = results[0]["error"]
            err_txt = first if isinstance(first, str) else (first or {}).get(
                "message", "unknown tool error")
            yield {"type": "thought",
                   "text": "The tool ran but returned an error result. The honest move is to "
                           "report it and suggest a retry \u2014 never fabricate weather data."}
            yield {"type": "answer",
                   "text": "\u26a0\ufe0f I couldn't fetch live weather data.\n\n`" + str(err_txt)
                           + "`\n\nCheck the spelling of the place, or try again in a moment."}
            return

        if plural:
            observe = ("All results are in. I'll line up the key numbers side by side "
                       "\u2014 temperature, condition, rain chance \u2014 and add a one-line verdict.")
        else:
            d = ok[0]["data"]
            cur = d.get("current") or {}
            today = d.get("today") or {}
            u = d.get("units") or {}
            observe = ("The tool answered with structured JSON \u2014 exactly the 'context "
                       "enhancement' I needed. Key fields: "
                       f"{_fmt(cur.get('temperature'))}{u.get('temperature', '')} and "
                       f"{str(cur.get('condition', '')).lower()}, humidity "
                       f"{_fmt(cur.get('humidity_pct'))}%, today's max rain chance "
                       f"{_fmt(today.get('max_rain_chance_pct'))}%. "
                       "No further tool calls required \u2014 time to write the summary myself.")
        yield {"type": "thought", "text": observe}
        yield {"type": "answer", "text": compose_answer(ok, want_compare=plural)}

    def _call(self, args: dict):
        frames = []

        def log(direction, payload):
            frames.append({"dir": direction, "payload": payload})

        old = self.client.log
        self.client.log = log
        try:
            data, is_err = self.client.call_tool(TOOL, args)
        finally:
            self.client.log = old
        return frames, data, is_err


# --------------------------------------------------------------------------- #
# Optional: a REAL LLM doing genuine function-calling.
# Talks to the exact same tool server — only the "brain" changes.
# Primary provider: Groq (https://console.groq.com) — free tier, blazing fast,
# speaks the OpenAI chat-completions dialect with tool calling.
# --------------------------------------------------------------------------- #
class OpenAICompatLLM:
    """Works with any OpenAI-compatible chat-completions endpoint
    (Groq, OpenAI, Together, vLLM, LM Studio…)."""

    def __init__(self, client: ConnectorClient, *, label: str, key: str,
                 model: str, api: str):
        self.client = client
        self.key = key
        self.model = model
        self.api = api
        self.name = f"{label} · {model} (real function-calling)"

    @property
    def available(self) -> bool:
        return bool(self.key)

    def respond(self, user_msg: str, session: dict):
        if not self.available:
            yield from SimulatedLLM(self.client).respond(user_msg, session)
            return
        yield {"type": "status", "text": f"Asking {self.model}\u2026"}
        descriptor = None
        for t in self.client.tools or []:
            if t.get("name") == TOOL:
                descriptor = t
        if descriptor is None:
            _, self.client.tools = self.client.handshake()
            descriptor = next(t for t in self.client.tools if t.get("name") == TOOL)

        tools_api = [{"type": "function", "function": {
            "name": descriptor["name"],
            "description": descriptor["description"],
            "parameters": descriptor["inputSchema"],
        }}]
        system = ("You are a weather dashboard demo agent. You have exactly one tool: "
                  f"{TOOL}. For any live-weather question, call it, then summarise the "
                  "JSON result in 2-6 short markdown lines with the key numbers. "
                  "Never invent numbers. If the tool errors, say so honestly.")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user_msg}]

        for _ in range(4):
            reply = self._chat(messages, tools_api)
            m = reply["choices"][0]["message"]
            if m.get("tool_calls"):
                if m.get("content"):
                    yield {"type": "thought", "text": m["content"]}
                messages.append(m)
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    name = fn.get("name", TOOL)
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    yield {"type": "tool_call", "tool": name, "arguments": args}
                    frames, data, is_err = self._exec(name, args)
                    for fr in frames:
                        yield {"type": "rpc", **fr}
                    yield {"type": "tool_result", "tool": name,
                           "ok": not is_err, "data": data}
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                     "content": json.dumps(data)[:6000]})
                continue
            yield {"type": "thought",
                   "text": "The model decided it has everything it needs and wrote the "
                           "final answer \u2014 no more tool calls."}
            yield {"type": "answer", "text": m.get("content") or "(empty response)"}
            return
        yield {"type": "answer", "text": "\u26a0\ufe0f Gave up after 4 model turns."}

    def _chat(self, messages, tools_api):
        body = json.dumps({"model": self.model, "messages": messages,
                           "tools": tools_api}).encode("utf-8")
        req = urllib.request.Request(self.api, data=body, headers={
            "Content-Type": "application/json",
            # Groq sits behind Cloudflare, which rejects the default
            # "Python-urllib" User-Agent with error 1010 -> HTTP 403.
            "User-Agent": "data-dashboard-connector/1.0 (weather lab)",
            "Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _exec(self, name, args):
        frames = []

        def log(direction, payload):
            frames.append({"dir": direction, "payload": payload})

        old = self.client.log
        self.client.log = log
        try:
            if name != TOOL:
                data, is_err = f"unknown tool '{name}'", True
            else:
                data, is_err = self.client.call_tool(TOOL, args)
        finally:
            self.client.log = old
        return frames, data, is_err


class GroqLLM(OpenAICompatLLM):
    """GroqCloud — default REAL brain. Key from GROQ_API_KEY or groq_api_key.txt."""
    provider = "Groq"

    def __init__(self, client: ConnectorClient):
        super().__init__(
            client,
            label="Groq",
            key=_key("GROQ_API_KEY", "groq_api_key.txt"),
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            api=os.environ.get("GROQ_BASE_URL",
                               "https://api.groq.com/openai/v1/chat/completions"),
        )


class OpenAILLM(OpenAICompatLLM):
    """Alternative brain: OpenAI itself, or any compatible endpoint."""
    provider = "OpenAI"

    def __init__(self, client: ConnectorClient):
        super().__init__(
            client,
            label="OpenAI",
            key=_key("OPENAI_API_KEY", "openai_api_key.txt"),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            api=os.environ.get("OPENAI_BASE_URL",
                               "https://api.openai.com/v1/chat/completions"),
        )


# --------------------------------------------------------------------------- #
# Agent = client + brain. Its handle() generator IS the visible thought process.
# --------------------------------------------------------------------------- #
class Agent:
    def __init__(self, client: ConnectorClient, llm=None):
        self.client = client
        self.llm = llm or SimulatedLLM(client)

    @property
    def llm_name(self) -> str:
        return getattr(self.llm, "name", str(type(self.llm).__name__))

    def handle(self, user_msg: str, session: dict):
        try:
            yield from self.llm.respond(user_msg, session)
        except Exception as e:  # LLM backend blew up -> fall back gracefully
            yield {"type": "thought",
                   "text": f"The configured LLM backend failed ({e!r}). Falling back to "
                           "the built-in rule-based reasoner so the demo still works."}
            session = dict(session)
            yield from SimulatedLLM(self.client).respond(user_msg, session)
