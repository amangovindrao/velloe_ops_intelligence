"""
Operational data sources for the Velloe Scout.

  primary  : simulated "Velloe Ops API v2" (JSON text, UTC timestamps, nested,
             different field names). Can be made to fail via the FaultInjector.
  fallback : local flat snapshot export (data/velloe_ops_snapshot.json, IST).

Both return DIFFERENT shapes on purpose; normalize() converts either into one
common record schema, so the Analyst never sees source-specific formats.
Tool functions raise on failure. Retry/timeout/fallback policy lives in the
Scout (agents/scout.py), not here.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from config import OPS_DATA_PATH, SIM_API_LATENCY_SECONDS

IST = timezone(timedelta(hours=5, minutes=30))
ENDPOINTS = ("incidents", "telemetry", "maintenance")


class OpsAPIError(Exception):
    def __init__(self, code, message, retry_after=None):
        super().__init__(f"HTTP {code} {message}")
        self.code = code
        self.retry_after = retry_after


class UnexpectedSchemaError(ValueError):
    """The response parsed as JSON but did not match the documented tool schema."""


class NoDataError(ValueError):
    """The response was valid but contained no usable records for the request."""


def load_snapshot():
    with open(OPS_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def site_registry():
    """Static reference data (site codes, aliases, coordinates). Empty dict if unreadable."""
    try:
        return load_snapshot()["sites"]
    except Exception:
        return {}


def dataset_window():
    try:
        return dict(load_snapshot()["_meta"]["window"])
    except Exception:
        return {"start": "2024-06-09", "end": "2024-06-14"}


def parse_time(value):
    """ISO string (with Z or offset) or epoch seconds -> aware datetime in IST.
    A timestamp without an offset is read as IST (the dataset's documented zone), never as the host's
    local zone - otherwise results changed between an IST laptop and a UTC CI machine."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(IST)
    text = str(value).strip()
    if text.lstrip("-").replace(".", "", 1).isdigit():  # epoch seconds sent as a string
        return datetime.fromtimestamp(float(text), tz=timezone.utc).astimezone(IST)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _iso(dt):
    return dt.astimezone(IST).isoformat()


def _utc(value):
    return parse_time(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def telemetry_id(site, sensor, dt):
    """Unique per reading: includes the year, and seconds when non-zero (MMDDHHMM alone collided for
    readings in the same minute or a year apart, silently merging evidence)."""
    dt = dt.astimezone(IST)
    stamp = dt.strftime("%Y%m%d%H%M") + (dt.strftime("%S") if dt.second else "")
    return f"TEL-{site}-{sensor}-{stamp}"


# ---------------------------------------------------------------- primary (simulated API)
def ops_api_fetch(endpoint, faults, timeout):
    """Simulated Ops API call. Returns raw JSON TEXT (like a real HTTP body)."""
    fault = faults.next(endpoint)
    time.sleep(SIM_API_LATENCY_SECONDS)
    if fault == "down":
        raise ConnectionError("connection refused: ops-api.velloe.local:443")
    if fault == "500":
        raise OpsAPIError(500, "Internal Server Error")
    if fault == "429":
        raise OpsAPIError(429, "Too Many Requests", retry_after=0)
    if fault == "timeout":
        time.sleep(timeout + 0.3)  # outlives the Scout's deadline -> real timeout
    snap = load_snapshot()
    if fault == "malformed":
        return '{"apiVersion": "2.1", "status": "ok", "data": {"items": [{"incidentId": "INC-10'
    if fault == "schema":
        return json.dumps({"apiVersion": "2.1", "status": "ok", "data": {"unexpected": []}})

    if endpoint == "incidents":
        sev = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
        items = [
            {"incidentId": r["id"], "siteCode": r["site"], "openedAt": _utc(r["opened"]),
             "sev": sev.get(r["severity"], 4), "cat": r["category"].upper(), "title": r["summary"]}
            for r in snap["incidents"]
        ]
        body = {"apiVersion": "2.1", "status": "ok", "data": {"items": items, "count": len(items)}}
    elif endpoint == "telemetry":
        series = {}
        for r in snap["telemetry"]:
            key = (r["site"], r["sensor"], r["metric"], r.get("unit"))
            series.setdefault(key, []).append([int(parse_time(r["time"]).timestamp()), r["value"]])
        body = {"apiVersion": "2.1", "status": "ok", "data": {"series": [
            {"site": s, "sensor": sn, "metric": m, "unit": u, "points": pts}
            for (s, sn, m, u), pts in series.items()]}}
    elif endpoint == "maintenance":
        body = {"apiVersion": "2.1", "status": "ok", "data": {"changes": [
            {"ref": r["id"], "site": r["site"], "asset": r["asset"], "kind": r["type"],
             "window": {"from": _utc(r["start"]), "to": _utc(r["end"])}, "notes": r["description"]}
            for r in snap["maintenance"]]}}
    else:
        raise OpsAPIError(404, f"unknown endpoint {endpoint}")
    if fault == "no_data":
        key = {"incidents": "items", "telemetry": "series", "maintenance": "changes"}[endpoint]
        body["data"][key] = []
        if endpoint == "incidents":
            body["data"]["count"] = 0
    return json.dumps(body)


# ---------------------------------------------------------------- fallback (snapshot export)
def snapshot_fetch(endpoint, faults, timeout):
    fault = faults.next(f"snapshot:{endpoint}")
    if fault in ("down", "500", "timeout"):
        raise FileNotFoundError(f"snapshot export unavailable for '{endpoint}'")
    if not os.path.exists(OPS_DATA_PATH):
        raise FileNotFoundError(OPS_DATA_PATH)
    return {"format": "flat-snapshot-v1", "rows": load_snapshot()[endpoint]}


# ---------------------------------------------------------------- normalization
def normalize(endpoint, payload):
    """Either source shape -> common records with explicit schema/no-data errors."""
    if isinstance(payload, str):
        payload = json.loads(payload)  # malformed JSON remains distinguishable
    if not isinstance(payload, dict):
        raise UnexpectedSchemaError("payload is not a JSON object")

    try:
        if payload.get("format") == "flat-snapshot-v1":
            rows = payload["rows"]
            if not isinstance(rows, list):
                raise UnexpectedSchemaError("snapshot rows is not a list")
            out = [_norm_snapshot(endpoint, r) for r in rows]
        else:
            if payload.get("status") != "ok" or not isinstance(payload.get("data"), dict):
                raise UnexpectedSchemaError(f"unexpected API envelope: keys={list(payload)}")
            data = payload["data"]
            if endpoint == "incidents":
                items = data["items"]
                if not isinstance(items, list):
                    raise UnexpectedSchemaError("incident items is not a list")
                out = [{"id": i["incidentId"], "type": "incident", "site": i["siteCode"],
                        "time": _iso(parse_time(i["openedAt"])), "severity": f"P{i['sev']}",
                        "category": i["cat"].lower(), "summary": i["title"]} for i in items]
            elif endpoint == "telemetry":
                series = data["series"]
                if not isinstance(series, list):
                    raise UnexpectedSchemaError("telemetry series is not a list")
                out = []
                for s in series:
                    for ts, value in s["points"]:
                        dt = parse_time(ts)
                        out.append({"id": telemetry_id(s["site"], s["sensor"], dt), "type": "telemetry",
                                    "site": s["site"], "sensor": s["sensor"], "metric": s["metric"],
                                    "time": _iso(dt), "value": float(value), "unit": s.get("unit")})
            elif endpoint == "maintenance":
                changes = data["changes"]
                if not isinstance(changes, list):
                    raise UnexpectedSchemaError("maintenance changes is not a list")
                out = [{"id": c["ref"], "type": "maintenance", "site": c["site"], "asset": c["asset"],
                        "kind": c["kind"], "start": _iso(parse_time(c["window"]["from"])),
                        "end": _iso(parse_time(c["window"]["to"])), "description": c["notes"]}
                       for c in changes]
            else:
                raise UnexpectedSchemaError(f"no normalizer for endpoint '{endpoint}'")
    except UnexpectedSchemaError:
        raise
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise UnexpectedSchemaError(f"unexpected {endpoint} schema: {type(exc).__name__}: {exc}") from exc
    if not out:
        raise NoDataError(f"{endpoint} response was valid but contained no records")
    return out


def _norm_snapshot(endpoint, r):
    if endpoint == "incidents":
        return {"id": r["id"], "type": "incident", "site": r["site"], "time": _iso(parse_time(r["opened"])),
                "severity": r["severity"], "category": r["category"], "summary": r["summary"]}
    if endpoint == "telemetry":
        dt = parse_time(r["time"])
        return {"id": telemetry_id(r["site"], r["sensor"], dt), "type": "telemetry", "site": r["site"],
                "sensor": r["sensor"], "metric": r["metric"], "time": _iso(dt), "value": float(r["value"]),
                "unit": r.get("unit")}
    if endpoint == "maintenance":
        return {"id": r["id"], "type": "maintenance", "site": r["site"], "asset": r["asset"],
                "kind": r["type"], "start": _iso(parse_time(r["start"])), "end": _iso(parse_time(r["end"])),
                "description": r["description"]}
    raise ValueError(f"no normalizer for endpoint '{endpoint}'")
