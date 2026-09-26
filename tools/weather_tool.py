"""
Real external tool: Open-Meteo historical weather archive (free, no API key).
Fallback: the cached Open-Meteo export stored in the ops snapshot.
Both return the Open-Meteo "daily" shape; normalize() turns it into records.
"""
import json

import requests

from tools.ops_api import NoDataError, OpsAPIError, UnexpectedSchemaError, load_snapshot

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def request_metadata(site, lat, lon, start, end):
    """Allowlisted request metadata safe for result and audit output."""
    return {"tool": "open-meteo", "method": "GET", "url": ARCHIVE_URL, "site": site,
            "params": {"latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
                       "daily": "temperature_2m_max,temperature_2m_min", "timezone": "Asia/Kolkata"}}


def open_meteo_fetch(site, lat, lon, start, end, faults, timeout):
    request = request_metadata(site, lat, lon, start, end)
    fault = faults.next("weather", scope=site)  # each site gets the full simulated fault sequence
    if fault == "down":
        raise ConnectionError("simulated outage: archive-api.open-meteo.com unreachable")
    if fault == "500":
        raise OpsAPIError(500, "Internal Server Error (simulated)")
    if fault == "429":
        raise OpsAPIError(429, "Too Many Requests (simulated)", retry_after=0)
    if fault == "timeout":
        raise requests.Timeout(f"read timed out after {timeout}s (simulated)")
    if fault == "malformed":
        return {"_transport": {"request": request, "response_status": 502},
                "body": "<html><body>502 Bad Gateway</body></html>"}
    if fault == "schema":
        return {"_transport": {"request": request, "response_status": 200}, "body": '{"hourly": {}}'}
    if fault == "no_data":
        return {"_transport": {"request": request, "response_status": 200},
                "body": '{"daily":{"time":[],"temperature_2m_max":[],"temperature_2m_min":[]}}'}
    resp = requests.get(ARCHIVE_URL, params=request["params"], timeout=timeout)
    resp.raise_for_status()
    return {"_transport": {"request": request, "response_status": resp.status_code}, "body": resp.text}


def weather_cache_fetch(site, lat, lon, start, end, faults, timeout):
    if faults.next("weather_cache", scope=site):
        raise FileNotFoundError("weather cache unavailable")
    cache = load_snapshot()["weather_cache"]
    if site not in cache:
        raise KeyError(f"no cached weather for {site}")
    return {"daily": cache[site]}


def normalize(site, payload, start, end):
    if isinstance(payload, dict) and "_transport" in payload:
        payload = payload.get("body")
    if isinstance(payload, str):
        payload = json.loads(payload)  # malformed body remains distinguishable
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        raise UnexpectedSchemaError("Open-Meteo response is missing the daily object")
    daily = payload["daily"]
    try:
        days, highs, lows = daily["time"], daily["temperature_2m_max"], daily["temperature_2m_min"]
        if not all(isinstance(values, list) for values in (days, highs, lows)) \
                or not (len(days) == len(highs) == len(lows)):
            raise UnexpectedSchemaError("Open-Meteo daily arrays are missing, invalid, or different lengths")
        out = []
        for day, tmax, tmin in zip(days, highs, lows):
            if start <= day <= end and tmax is not None:
                out.append({"id": f"WX-{site}-{day}", "type": "weather", "site": site, "date": day,
                            "tmax": float(tmax), "tmin": float(tmin) if tmin is not None else None})
    except UnexpectedSchemaError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise UnexpectedSchemaError(f"unexpected Open-Meteo daily schema: {type(exc).__name__}: {exc}") from exc
    if not out:
        raise NoDataError("Open-Meteo response was valid but contained no days in the requested window")
    return out
