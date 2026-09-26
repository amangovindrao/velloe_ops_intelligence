"""
Velloe Scout - "What information can we reliably obtain?"

Executes the Strategist's data tasks. Owns the reliability contract:
  retry (bounded) -> caller deadline per attempt -> malformed-response detection
  -> fallback source -> graceful degradation (status "failed", no fabricated data).
Every attempt is written to the audit trail and the tool_executions table. A deadline
stops the workflow waiting for a call; Python worker threads cannot be force-killed.
"""
import concurrent.futures
import threading
import time

import requests

from config import (SCOUT_BACKOFF_SECONDS, SCOUT_MAX_ATTEMPTS, SCOUT_TIMEOUT_SECONDS,
                    WEATHER_TIMEOUT_SECONDS)
from tools import ops_api, weather_tool

MALFORMED = (ValueError, KeyError, TypeError, IndexError)
MAX_RETRY_AFTER_SECONDS = 2.0


def _with_timeout(fn, timeout):
    """Run fn(timeout) in its own thread and stop waiting after `timeout`; the thread cannot be killed.
    A dedicated thread per attempt (not a shared pool) means time spent queued behind other, hung calls
    can never be counted against a healthy call's deadline."""
    box = {}

    def work():
        try:
            box["value"] = fn(timeout)
        except BaseException as e:  # re-raised on the caller thread
            box["error"] = e

    worker = threading.Thread(target=work, daemon=True, name="scout-call")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise concurrent.futures.TimeoutError(f"no response within {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _classify(exc):
    if isinstance(exc, (concurrent.futures.TimeoutError, TimeoutError, requests.Timeout)):
        return "timeout", "timed out"
    if isinstance(exc, ops_api.NoDataError):
        return "no_data", str(exc)[:160]
    if isinstance(exc, ops_api.UnexpectedSchemaError):
        return "unexpected_schema", str(exc)[:160]
    if isinstance(exc, ops_api.OpsAPIError):
        return f"http_{exc.code}", str(exc)[:160]
    if isinstance(exc, requests.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return f"http_{code}" if code else "http_error", str(exc)[:160]
    if isinstance(exc, (requests.ConnectionError, ConnectionError)):
        return "connection_error", str(exc)[:160]
    if isinstance(exc, MALFORMED):
        return "malformed_response", f"malformed response ({type(exc).__name__}: {str(exc)[:80]})"
    return "error", str(exc)[:160]


def _retry_delay(exc, attempt):
    """Honor Retry-After for 429s, but clamp it so retries always remain bounded."""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None and isinstance(exc, requests.HTTPError):
        retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
    try:
        retry_after = float(retry_after) if retry_after is not None else 0
    except (TypeError, ValueError):
        retry_after = 0
    return min(MAX_RETRY_AFTER_SECONDS, max(SCOUT_BACKOFF_SECONDS * attempt, retry_after))


def _transport(payload, default_request, default_status="200"):
    meta = payload.get("_transport", {}) if isinstance(payload, dict) else {}
    return meta.get("request", default_request), meta.get("response_status", default_status)


def _normalized_summary(records):
    return {"count": len(records), "record_ids": [r.get("id") for r in records[:20]]}


class Scout:
    def __init__(self, bb, faults):
        self.bb, self.faults = bb, faults

    # ------------------------------------------------------------------ public
    def collect(self, task, plan):
        tool = task["tool"]
        if tool in ops_api.ENDPOINTS:
            result = self._resilient(
                tool,
                primary=("Velloe Ops API", lambda t: ops_api.ops_api_fetch(tool, self.faults, t)),
                fallback=("snapshot export", lambda t: ops_api.snapshot_fetch(tool, self.faults, t)),
                normalize=lambda p: ops_api.normalize(tool, p),
                timeout=SCOUT_TIMEOUT_SECONDS,
                primary_request={"tool": tool, "method": "GET", "url": f"simulated://velloe-ops/v2/{tool}"},
                fallback_request={"tool": tool, "method": "READ", "source": "local snapshot export"},
            )
            result["records"] = _in_scope(result["records"], plan)
            return _attach_missing_fields(result, tool)
        if tool == "weather":
            return self._weather(plan)
        raise ValueError(f"unknown tool {tool}")

    # ------------------------------------------------------------------ internals
    def _weather(self, plan):
        registry = ops_api.site_registry()
        start, end = plan["window"]["start"], plan["window"]["end"]
        parts = []
        for site in plan["sites"]:
            s = registry.get(site)
            if not s:
                continue
            parts.append(self._resilient(
                f"weather:{site}",
                primary=("Open-Meteo archive API", lambda t, s=s, site=site: weather_tool.open_meteo_fetch(
                    site, s["lat"], s["lon"], start, end, self.faults, t)),
                fallback=("cached Open-Meteo export", lambda t, s=s, site=site: weather_tool.weather_cache_fetch(
                    site, s["lat"], s["lon"], start, end, self.faults, t)),
                normalize=lambda p, site=site: weather_tool.normalize(site, p, start, end),
                timeout=WEATHER_TIMEOUT_SECONDS,
                primary_request=weather_tool.request_metadata(site, s["lat"], s["lon"], start, end),
                fallback_request={"tool": "weather", "method": "READ", "source": "cached Open-Meteo export",
                                  "site": site, "start": start, "end": end},
            ))
        order = {"ok": 0, "degraded_fallback": 1, "failed": 2}
        worst = max(parts, key=lambda r: order[r["status"]]) if parts else {"status": "failed", "source": None}
        result = {"tool": "weather", "status": worst["status"],
                  "source": ", ".join(sorted({p["source"] for p in parts if p["source"]})) or None,
                  "records": [r for p in parts for r in p["records"]],
                  "attempts": sum(p["attempts"] for p in parts),
                  "errors": [e for p in parts for e in p["errors"]]
                            or (["no requested site exists in the site registry"] if not parts else []),
                  "retries": sum(p["retries"] for p in parts),
                  "fallback_used": any(p["fallback_used"] for p in parts),
                  "request": worst.get("request"), "response_status": worst.get("response_status"),
                  "validation": worst.get("validation"),
                  "normalized_result": _normalized_summary([r for p in parts for r in p["records"]])}
        return _attach_missing_fields(result, "weather")

    def _resilient(self, tool, primary, fallback, normalize, timeout,
                   primary_request=None, fallback_request=None):
        bb, errors = self.bb, []
        p_name, p_fn = primary
        for attempt in range(1, SCOUT_MAX_ATTEMPTS + 1):
            label = "Calling" if attempt == 1 else f"Retry {attempt - 1}:"
            t0 = time.time()
            try:
                payload = _with_timeout(p_fn, timeout)
                request, response_status = _transport(payload, primary_request)
                records = normalize(payload)
                normalized = _normalized_summary(records)
                ms = int((time.time() - t0) * 1000)
                bb.tool_call(tool, p_name, attempt, "ok", duration_ms=ms, response_status=response_status,
                             validation="validated", records_count=len(records))
                bb.log("scout", "tool_call", request or f"{p_name} /{tool}", normalized,
                       reason="Primary source answered; schema validation and normalization succeeded.",
                       duration_ms=ms, retry=attempt - 1, tool=f"{p_name} /{tool}", request=request,
                       response_status=response_status, validation="validated", normalized_result=normalized)
                return {"tool": tool, "status": "ok", "source": p_name, "records": records,
                        "attempts": attempt, "errors": errors, "retries": attempt - 1, "fallback_used": False,
                        "request": request, "response_status": str(response_status), "validation": "validated",
                        "normalized_result": normalized}
            except Exception as exc:
                ms = int((time.time() - t0) * 1000)
                outcome, detail = _classify(exc)
                errors.append(f"attempt {attempt}: {outcome}: {detail}")
                bb.tool_call(tool, p_name, attempt, outcome, error=detail, duration_ms=ms, response_status=outcome,
                             validation=f"failed:{outcome}", records_count=0)
                last = attempt == SCOUT_MAX_ATTEMPTS
                bb.log("scout", "tool_failure", primary_request or f"{p_name} /{tool}", detail,
                       reason="Retry limit reached, switching to fallback source." if last
                       else f"Transient failure ({outcome}); retrying with bounded backoff.",
                       status="warn", duration_ms=ms, retry=attempt - 1, tool=f"{p_name} /{tool}",
                       request=primary_request, response_status=outcome, validation=f"failed:{outcome}",
                       normalized_result={"count": 0, "record_ids": []})
                if not last:
                    time.sleep(_retry_delay(exc, attempt))

        f_name, f_fn = fallback
        t0 = time.time()
        try:
            payload = _with_timeout(f_fn, timeout)
            request, response_status = _transport(payload, fallback_request, "local")
            records = normalize(payload)
            normalized = _normalized_summary(records)
            ms = int((time.time() - t0) * 1000)
            bb.tool_call(tool, f_name, SCOUT_MAX_ATTEMPTS + 1, "fallback_ok", duration_ms=ms,
                         response_status=response_status, validation="validated", fallback_used=True,
                         records_count=len(records))
            bb.log("scout", "fallback", request or f"{f_name} /{tool}", normalized,
                   reason=f"Primary failed {SCOUT_MAX_ATTEMPTS}x; validated fallback activated.",
                   status="recovered", duration_ms=ms, retry=SCOUT_MAX_ATTEMPTS - 1, tool=f"{f_name} /{tool}",
                   fallback_used=True, request=request, response_status=response_status, validation="validated",
                   normalized_result=normalized)
            return {"tool": tool, "status": "degraded_fallback", "source": f_name, "records": records,
                    "attempts": SCOUT_MAX_ATTEMPTS + 1, "errors": errors,
                    "retries": SCOUT_MAX_ATTEMPTS - 1, "fallback_used": True, "request": request,
                    "response_status": str(response_status), "validation": "validated",
                    "normalized_result": normalized}
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            outcome, detail = _classify(exc)
            errors.append(f"fallback: {outcome}: {detail}")
            bb.tool_call(tool, f_name, SCOUT_MAX_ATTEMPTS + 1, "failed", error=detail, duration_ms=ms,
                         response_status=outcome, validation=f"failed:{outcome}", fallback_used=True,
                         records_count=0)
            bb.log("scout", "degraded", fallback_request or f"{f_name} /{tool}", detail,
                   reason="Primary and fallback both failed. Continuing WITHOUT this data; nothing is fabricated.",
                   status="error", duration_ms=ms, retry=SCOUT_MAX_ATTEMPTS - 1, tool=f"{f_name} /{tool}",
                   fallback_used=True, request=fallback_request, response_status=outcome,
                   validation=f"failed:{outcome}", normalized_result={"count": 0, "record_ids": []})
            return {"tool": tool, "status": "failed", "source": None, "records": [],
                    "attempts": SCOUT_MAX_ATTEMPTS + 1, "errors": errors,
                    "retries": SCOUT_MAX_ATTEMPTS - 1, "fallback_used": True,
                    "request": fallback_request, "response_status": outcome,
                    "validation": f"failed:{outcome}",
                    "normalized_result": {"count": 0, "record_ids": []}}


# ---------------------------------------------------------------------- individual mode helpers
TOOL_LABELS = {"incidents": "Incident API (Velloe Ops API, simulated)",
               "telemetry": "Telemetry API (Velloe Ops API, simulated)",
               "maintenance": "Maintenance API (Velloe Ops API, simulated)",
               "weather": "Open-Meteo archive API (real, external)"}
TOOL_KEYWORDS = {
    "incidents": ("incident", "alert", "outage", "ticket", "event"),
    "telemetry": ("telemetry", "temperature", "sensor", "reading", "inlet", "metric"),
    "maintenance": ("maintenance", "change", "work order", "firmware", "repair", "upgrade"),
    "weather": ("weather", "external", "outdoor", "open-meteo", "climate"),
}
# Fields the operator schema expects. Anything absent from every record is reported as unavailable.
EXPECTED_FIELDS = {
    "incidents": ("id", "site", "time", "severity", "category", "summary", "resolved_at", "assigned_team"),
    "telemetry": ("id", "site", "sensor", "metric", "time", "value", "unit"),
    "maintenance": ("id", "site", "asset", "kind", "start", "end", "description", "approved_by"),
    "weather": ("id", "site", "date", "tmax", "tmin"),
}
INSUFFICIENT = "Insufficient evidence. No reliable operational data is available."


def _missing_fields(tool, records):
    """Return record-level normalized-schema gaps; never invent replacement values."""
    expected = EXPECTED_FIELDS.get(tool, ())
    if not records:
        return [{"record_id": None, "record_index": None, "fields": list(expected),
                 "reason": "no records returned"}]
    missing = []
    for index, record in enumerate(records):
        fields = [field for field in expected if record.get(field) in (None, "")]
        if fields:
            missing.append({"record_id": record.get("id"), "record_index": index, "fields": fields,
                            "reason": "field absent or null after normalization"})
    return missing


def _attach_missing_fields(result, tool):
    result["missing_fields"] = _missing_fields(tool, result["records"])
    return result


def choose_tools(request, preferred=None):
    """Pick data sources for a free-text request. `preferred` wins if given."""
    if preferred:
        return [preferred]
    from agents.strategist import kw  # same word-start matching ('prevent' is not 'event')

    q = request.lower()
    tools = [t for t, kws in TOOL_KEYWORDS.items() if any(kw(k, q) for k in kws)]
    return tools or ["incidents", "telemetry"]


def describe(result):
    """Operator-facing summary of one tool result: status, missing fields, retries, fallback, notes."""
    tool = result["tool"]
    recs = result["records"]
    gaps = result.get("missing_fields") or _missing_fields(tool, recs)
    missing = sorted({field for gap in gaps for field in gap["fields"]})
    notes = {
        "ok": "Primary source answered; payload validated and normalized.",
        "degraded_fallback": "Primary source failed; data served from the fallback source and may be stale.",
        "failed": "Primary and fallback sources failed. No data returned; nothing was fabricated.",
    }[result["status"]]
    if tool == "weather" and result["status"] == "ok":
        notes += " Live response from a real external API."
    if tool != "weather":
        notes += " Operational records are simulated demo data."
    return {
        "tool": tool, "tool_label": TOOL_LABELS.get(tool, tool), "source": result["source"] or "unavailable",
        "status": result["status"], "records": len(recs), "missing_fields": missing,
        "retries": result.get("retries", 0), "fallback_used": result.get("fallback_used", False),
        "errors": result["errors"], "notes": notes,
        "selection_reason": result.get("selection_reason") or "The source was explicitly selected for this task.",
        "message": INSUFFICIENT if not recs else None,
    }


def _in_scope(records, plan):
    sites = set(plan.get("sites") or [])
    start, end = plan["window"]["start"], plan["window"]["end"]
    out = []
    for r in records:
        if sites and r.get("site") not in sites:
            continue
        day = (r.get("time") or r.get("start") or r.get("date") or "")[:10]
        # maintenance overlapping the window also counts
        last_day = (r.get("end") or day)[:10]
        if day > end or last_day < start:
            continue
        out.append(r)
    return out
