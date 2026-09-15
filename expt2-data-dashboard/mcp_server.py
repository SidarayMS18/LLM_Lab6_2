"""
Expt 2 — The "Data Dashboard" Connector
=======================================
TOOL SERVER (the "server" role in the client-server pair).

It exposes exactly ONE tool over JSON-RPC 2.0 / HTTP:

    get_current_weather(location: str, units: "metric" | "imperial")
        -> structured JSON (returned as text content, MCP-style)

Live data comes from wttr.in (free, no API key). If wttr.in is down or
rate-limited, the server transparently falls back to Open-Meteo (also free,
no key). Results are cached for WEATHER_CACHE_TTL seconds so we stay a polite
API citizen.

This mirrors what a real MCP (Model Context Protocol) server does:
    initialize          -> handshake, advertise capabilities
    tools/list          -> machine-readable tool catalog + JSON-Schema
    tools/call          -> actually execute the tool, return structured text
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "data-dashboard-connector", "version": "1.0.0"}

WTTR_BASE = "https://wttr.in"
HTTP_TIMEOUT = float(os.environ.get("WTTR_TIMEOUT", "8"))
CACHE_TTL = float(os.environ.get("WEATHER_CACHE_TTL", "600"))
FORCE_OFFLINE = os.environ.get("WEATHER_OFFLINE", "").lower() in ("1", "true", "yes")


class ToolError(Exception):
    """Raised when a tool cannot produce a result (bad args / upstream failure)."""


# --------------------------------------------------------------------------- #
# Tool catalog — the machine-readable contract the LLM plans against
# --------------------------------------------------------------------------- #
def tool_descriptor() -> dict:
    return {
        "name": "get_current_weather",
        "description": (
            "Fetch LIVE current weather for a city, plus a compact 3-day outlook. "
            "Returns structured JSON text: temperature, feels-like, condition, humidity, "
            "wind, rain chance, daily min/max, sunrise/sunset. Use this for ANY question "
            "about weather, temperature, rain, or 'should I carry an umbrella'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, e.g. 'Tokyo', 'Chennai, India', 'New York'.",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Measurement system (default: metric).",
                },
            },
            "required": ["location"],
        },
    }


TOOL_NAME = tool_descriptor()["name"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _dayname(iso_date: str) -> str:
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%a")
    except (ValueError, TypeError):
        return str(iso_date)


_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _wind_point(deg) -> str:
    try:
        return _POINTS[int(round((float(deg) % 360) / 22.5)) % 16]
    except (TypeError, ValueError):
        return "–"


# WMO weather-code -> human description (used by the Open-Meteo fallback)
WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Slight rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, heavy hail",
}


def _http_json(url: str, timeout: float, retries: int = 2) -> dict:
    """GET a URL and parse JSON, retrying transient failures (429/5xx/network)."""
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "data-dashboard-connector/1.0 (weather lab)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:160].strip()
            except Exception:
                pass
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(1.2)
                last_exc = e
                continue
            raise ToolError(f"data source returned HTTP {e.code} {detail}".strip()) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(1.2)
                last_exc = e
                continue
            raise ConnectionError(f"network unreachable: {e}") from e
    raise ToolError(f"data source unreachable: {last_exc!r}")


# --------------------------------------------------------------------------- #
# Offline sample (WEATHER_OFFLINE=1) — clearly marked as simulated
# --------------------------------------------------------------------------- #
_OFFLINE_BASE = {
    "current": {
        "observation_time_local": "12:00 PM", "condition": "Partly cloudy",
        "temperature": 24.0, "feels_like": 26.0, "humidity_pct": 68,
        "wind_speed": 12.0, "wind_direction": "SW", "precipitation": 0.0,
        "cloud_cover_pct": 45, "visibility": 10.0, "pressure_mb": 1010,
    },
    "today": {"max_temperature": 29.0, "min_temperature": 22.0,
              "max_rain_chance_pct": 25, "sunrise": "06:00 AM", "sunset": "06:15 PM"},
    "forecast": [
        {"day": "Thu", "max_temperature": 30.0, "min_temperature": 23.0,
         "max_rain_chance_pct": 20, "condition": "Sunny"},
        {"day": "Fri", "max_temperature": 29.0, "min_temperature": 23.0,
         "max_rain_chance_pct": 45, "condition": "Patchy rain nearby"},
    ],
}


def _offline_payload(location: str, units: str) -> dict:
    imperial = units == "imperial"

    def t(v): return round(v * 9 / 5 + 32, 1) if imperial else v

    def w(v): return round(v * 0.621371, 1) if imperial else v

    def p(v): return round(v / 25.4, 2) if imperial else v

    base = json.loads(json.dumps(_OFFLINE_BASE))  # deep copy
    cur, today = base["current"], base["today"]
    for key in ("temperature", "feels_like", "wind_speed", "precipitation", "visibility"):
        cur[key] = {"temperature": t, "feels_like": t, "wind_speed": w,
                    "precipitation": p, "visibility": w}[key](cur[key])
    for d in [today] + base["forecast"]:
        for key in ("max_temperature", "min_temperature"):
            if key in d:
                d[key] = t(d[key])
    return {
        "tool": TOOL_NAME, "source": "built-in sample", "simulated": True,
        "location_requested": location,
        "resolved_place": {"area": str(location).title(), "region": "", "country": "",
                           "latitude": None, "longitude": None, "timezone": ""},
        "units": _units_block(imperial),
        "current": cur, "today": today,
        "forecast": [dict(d, date="", day=d["day"]) for d in base["forecast"]],
        "fetched_at_utc": _now_utc(),
    }


def _units_block(imperial: bool) -> dict:
    return {
        "system": "imperial" if imperial else "metric",
        "temperature": "°F" if imperial else "°C",
        "wind": "mph" if imperial else "kph",
        "precipitation": "in" if imperial else "mm",
        "visibility": "mi" if imperial else "km",
    }


# --------------------------------------------------------------------------- #
# Source 1: wttr.in  (?format=j1)
# --------------------------------------------------------------------------- #
def _geolocate(location: str):
    """Best-effort canonical place lookup via Open-Meteo's free geocoder.

    wttr.in's own geo-resolution is quirky for bare city names (e.g. 'Tokyo' can
    match a weather station on the island of Shikinejima), so we resolve the
    canonical place first and query wttr with 'Name,CountryCode'. Returns a
    place dict, or None if the geocoder is unavailable (we then fall back to the
    raw location string — behaviour degrades, never breaks).
    """
    try:
        geo = _http_json(
            "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name="
            + quote(location), 6)
        hits = geo.get("results") or []
        if not hits:
            return None
        g = hits[0]
        cc = (g.get("country_code") or "").strip()
        query = f"{g.get('name', location)},{cc}".strip(",")
        return {"query": query,
                "area": (g.get("name") or location).strip(),
                "region": (g.get("admin1") or "").strip(),
                "country": (g.get("country") or "").strip(),
                "latitude": g.get("latitude"), "longitude": g.get("longitude"),
                "timezone": g.get("timezone") or ""}
    except Exception:
        return None


def _fetch_wttr(location: str, units: str) -> dict:
    meta = _geolocate(location) or {}
    query = meta.get("query") or location
    data = _http_json(f"{WTTR_BASE}/{quote(query)}?format=j1", HTTP_TIMEOUT)
    try:
        cc = data["current_condition"][0]
        area = data["nearest_area"][0]
        days = data["weather"]
        first = days[0]
    except (KeyError, IndexError, TypeError):
        raise ToolError(f"wttr.in returned no usable data for '{location}'")

    imperial = units == "imperial"

    def t(v): return round(_f(v) * 9 / 5 + 32, 1) if imperial else round(_f(v), 1)

    def w(v): return round(_f(v) * 0.621371, 1) if imperial else round(_f(v), 1)

    def p(v): return round(_f(v) / 25.4, 2) if imperial else round(_f(v), 2)

    def vis(v): return round(_f(v) * 0.621371, 1) if imperial else round(_f(v), 1)

    forecast = []
    for d in days[:3]:
        hours = d.get("hourly") or []
        rain = max((int(_f(h.get("chanceofrain"))) for h in hours), default=0)
        mid = hours[len(hours) // 2] if hours else {}
        cond = ((mid.get("weatherDesc") or [{}])[0]).get("value", "—").strip()
        forecast.append({
            "date": d.get("date"), "day": _dayname(d.get("date")),
            "max_temperature": t(d.get("maxtempC")),
            "min_temperature": t(d.get("mintempC")),
            "max_rain_chance_pct": rain,
            "condition": cond,
        })

    astro = (first.get("astronomy") or [{}])[0]
    wdesc = ((cc.get("weatherDesc") or [{}])[0]).get("value", "Unknown").strip()
    wttr_area = ((area.get("areaName") or [{}])[0]).get("value", location).strip()
    place_tz = ((area.get("timezone") or [{}])[0]).get("value", "")

    if meta:
        resolved = {k: v for k, v in meta.items() if k != "query"}
        resolved["wttr_matched_area"] = wttr_area  # what the weather source matched
    else:
        resolved = {
            "area": wttr_area,
            "region": ((area.get("region") or [{}])[0]).get("value", "").strip(),
            "country": ((area.get("country") or [{}])[0]).get("value", "").strip(),
            "latitude": _f(area.get("latitude"), 0.0),
            "longitude": _f(area.get("longitude"), 0.0),
            "timezone": place_tz,
        }

    return {
        "tool": TOOL_NAME, "source": "wttr.in", "simulated": False,
        "location_requested": location,
        "resolved_place": resolved,
        "units": _units_block(imperial),
        "current": {
            "observation_time_local": cc.get("observation_time", ""),
            "condition": wdesc,
            "temperature": t(cc.get("temp_C")),
            "feels_like": t(cc.get("FeelsLikeC")),
            "humidity_pct": int(_f(cc.get("humidity"))),
            "wind_speed": w(cc.get("windspeedKmph")),
            "wind_direction": cc.get("winddir16Point", ""),
            "precipitation": p(cc.get("precipMM")),
            "cloud_cover_pct": int(_f(cc.get("cloudcover"))),
            "visibility": vis(cc.get("visibility")),
            "pressure_mb": int(_f(cc.get("pressure"))),
        },
        "today": {
            "max_temperature": forecast[0]["max_temperature"] if forecast else None,
            "min_temperature": forecast[0]["min_temperature"] if forecast else None,
            "max_rain_chance_pct": forecast[0]["max_rain_chance_pct"] if forecast else 0,
            "sunrise": astro.get("sunrise", ""),
            "sunset": astro.get("sunset", ""),
        },
        "forecast": forecast,
        "fetched_at_utc": _now_utc(),
    }


# --------------------------------------------------------------------------- #
# Source 2 (fallback): Open-Meteo geocoding + forecast (free, key-less)
# --------------------------------------------------------------------------- #
def _fetch_open_meteo(location: str, units: str) -> dict:
    geo = _http_json(
        "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name="
        + quote(location), HTTP_TIMEOUT)
    hits = geo.get("results") or []
    if not hits:
        raise ToolError(f"geocoder found no place named '{location}'")
    g = hits[0]

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={g['latitude']}&longitude={g['longitude']}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
        "wind_speed_10m,wind_direction_10m,precipitation,cloud_cover,pressure_msl"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
        "weather_code,sunrise,sunset"
        "&forecast_days=3&wind_speed_unit=kmh&timezone=auto"
    )
    wdata = _http_json(url, HTTP_TIMEOUT)
    cur = wdata.get("current") or {}
    daily = wdata.get("daily") or {}

    imperial = units == "imperial"

    def t(v): return round(_f(v) * 9 / 5 + 32, 1) if imperial else round(_f(v), 1)

    def w(v): return round(_f(v) * 0.621371, 1) if imperial else round(_f(v), 1)

    def p(v): return round(_f(v) / 25.4, 2) if imperial else round(_f(v), 2)

    dates = daily.get("time") or []
    maxs = daily.get("temperature_2m_max") or []
    mins = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []
    codes = daily.get("weather_code") or []
    rises = daily.get("sunrise") or []
    sets = daily.get("sunset") or []

    forecast = []
    for i, date in enumerate(dates):
        forecast.append({
            "date": date, "day": _dayname(date),
            "max_temperature": t(maxs[i]) if i < len(maxs) else None,
            "min_temperature": t(mins[i]) if i < len(mins) else None,
            "max_rain_chance_pct": int(_f(rains[i])) if i < len(rains) else 0,
            "condition": WMO.get(codes[i] if i < len(codes) else -1, "Unknown"),
        })

    obs = str(cur.get("time", "")).split("T")
    return {
        "tool": TOOL_NAME, "source": "open-meteo", "simulated": False,
        "location_requested": location,
        "resolved_place": {
            "area": g.get("name", location), "region": g.get("admin1") or "",
            "country": g.get("country") or "",
            "latitude": g.get("latitude"), "longitude": g.get("longitude"),
            "timezone": g.get("timezone") or "",
        },
        "units": _units_block(imperial),
        "current": {
            "observation_time_local": obs[-1] if len(obs) > 1 else "",
            "condition": WMO.get(cur.get("weather_code"), "Unknown"),
            "temperature": t(cur.get("temperature_2m")),
            "feels_like": t(cur.get("apparent_temperature")),
            "humidity_pct": int(_f(cur.get("relative_humidity_2m"))),
            "wind_speed": w(cur.get("wind_speed_10m")),
            "wind_direction": _wind_point(cur.get("wind_direction_10m")),
            "precipitation": p(cur.get("precipitation")),
            "cloud_cover_pct": int(_f(cur.get("cloud_cover"))),
            "visibility": None,
            "pressure_mb": int(_f(cur.get("pressure_msl"))),
        },
        "today": {
            "max_temperature": forecast[0]["max_temperature"] if forecast else None,
            "min_temperature": forecast[0]["min_temperature"] if forecast else None,
            "max_rain_chance_pct": forecast[0]["max_rain_chance_pct"] if forecast else 0,
            "sunrise": str(rises[0]).split("T")[-1] if rises else "",
            "sunset": str(sets[0]).split("T")[-1] if sets else "",
        },
        "forecast": forecast,
        "fetched_at_utc": _now_utc(),
    }


# --------------------------------------------------------------------------- #
# Tool execution + cache
# --------------------------------------------------------------------------- #
_CACHE: dict = {}
_CACHE_LOCK = __import__("threading").Lock()


def call_tool(arguments: dict) -> dict:
    """Execute get_current_weather; returns an MCP-style content result."""
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be a JSON object")
    location = arguments.get("location")
    units = arguments.get("units", "metric")
    if not location or not str(location).strip():
        raise ToolError("missing required argument: location (string) — e.g. {'location': 'Tokyo'}")
    if units not in ("metric", "imperial"):
        raise ToolError("units must be 'metric' or 'imperial'")
    location = str(location).strip()

    key = (location.casefold(), units)
    now = time.time()
    payload = None
    cache_hit = False
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            payload = hit[1]
            cache_hit = True

    if payload is None:
        if FORCE_OFFLINE:
            payload = _offline_payload(location, units)
        else:
            errors = []
            for fetcher in (_fetch_wttr, _fetch_open_meteo):
                try:
                    payload = fetcher(location, units)
                    break
                except (ToolError, ConnectionError) as e:
                    errors.append(f"{fetcher.__name__}: {e}")
                except Exception as e:  # truly unexpected
                    errors.append(f"{fetcher.__name__}: unexpected {e!r}")
            if payload is None:
                raise ToolError("all data sources failed -> " + " | ".join(errors))
        with _CACHE_LOCK:
            _CACHE[key] = (now, payload)

    if cache_hit:
        payload = dict(payload)
        payload["cache"] = "hit"

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------------- #
# JSON-RPC 2.0 dispatcher
# --------------------------------------------------------------------------- #
def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def handle_request(payload):
    """Dispatch one JSON-RPC request. Returns a response dict, or None for notifications."""
    if not isinstance(payload, dict):
        return _err(None, -32600, "Invalid Request: body must be a JSON-RPC 2.0 object")
    method = payload.get("method") or ""
    req_id = payload.get("id")

    if method.startswith("notifications/"):
        return None  # notifications get no response (e.g. notifications/initialized)

    try:
        if method == "initialize":
            return _ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": "For any weather / temperature / rain question, "
                                "call the get_current_weather tool.",
            })
        if method == "ping":
            return _ok(req_id, {})
        if method == "tools/list":
            return _ok(req_id, {"tools": [tool_descriptor()]})
        if method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name")
            if name != TOOL_NAME:
                return _err(req_id, -32602,
                            f"Unknown tool '{name}'. Available tools: {TOOL_NAME}")
            try:
                return _ok(req_id, call_tool(params.get("arguments") or {}))
            except ToolError as e:
                # Tool ran but failed: report as an error RESULT so the LLM can react.
                return _ok(req_id, {"content": [{"type": "text", "text": str(e)}],
                                    "isError": True})
        return _err(req_id, -32601, f"Method not found: {method}")
    except Exception as e:
        return _err(req_id, -32603, f"Internal error: {e!r}")


if __name__ == "__main__":
    # Quick manual test from the CLI:  python3 mcp_server.py Tokyo
    import sys
    loc = sys.argv[1] if len(sys.argv) > 1 else "Tokyo"
    print(json.dumps(handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": TOOL_NAME, "arguments": {"location": loc}}}), indent=2))
