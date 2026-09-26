"""
Velloe Ops Intelligence - web dashboard server (Python stdlib only).

  python server.py            -> http://127.0.0.1:8000

Serves web/index.html + web/app.js and a small JSON API over the SQLite store.
Investigations run in background threads so the dashboard can poll progress.

SECURITY: no authentication. Binds to 127.0.0.1 by default; do not expose it
on a shared network without adding auth. Approvals only create SIMULATED
tickets in the local database.
"""
import json
import os
import re
import threading
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import store
from config import (AGENT_NAMES, DEMO_STEP_DELAY_SECONDS, EXECUTION_TIMEOUT, HOST, MAX_GUARDIAN_REVISIONS,
                    MAX_LLM_CALLS, MAX_REVISIONS, MAX_SECONDS, MAX_STEPS, MAX_TOOL_RETRIES,
                    MAX_WORKFLOW_STEPS, PORT, PRODUCT, ROOT, SCOUT_MAX_ATTEMPTS, SCOUT_TIMEOUT_SECONDS,
                    THERMAL_THRESHOLD_C)
from llm_client import active_provider
from orchestrator import (ACTIVE_STATUSES, AGENT_POLICY, HANDOFF_LABELS, Orchestrator, available_handoffs,
                          complete_action, decide_action, prepare_handoff, rerun_blocker)
from blackboard import INDIVIDUAL_AGENT
from tools.faults import AGENT_FAILURES, UI_FAILURES, UI_FAILURE_ALIASES
from tools.ops_api import dataset_window, load_snapshot

WEB = os.path.join(ROOT, "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript")}
RUNNING = {}  # investigation / execution id -> Thread
MAX_BODY = 40_000
FINAL = ("Approved", "Awaiting Approval", "Needs Review", "Completed")
WF = "i.mode = 'FULL_WORKFLOW'"  # workflow KPIs never count individual agent executions
CONTEXT_KEYS = ("records", "objective", "site", "start", "end", "preferred_source", "report", "evidence")


# ---------------------------------------------------------------------- helpers
RUN_LOCK = threading.Lock()  # RUNNING is shared by all request threads
HANDOFF_LOCK = threading.Lock()  # serializes "is it running? -> start it" decisions
CONFLICT = re.compile(r"not awaiting approval|already|concurrently|only Approved or Detected|human decision")


def _alive(inv_id):
    t = RUNNING.get(inv_id)
    return bool(t and t.is_alive())


def _live_ids():
    with RUN_LOCK:
        return [i for i, t in list(RUNNING.items()) if t.is_alive()]


def _text(value):
    """Request value -> str. None stays empty (str(None) became the literal task 'None');
    lists/dicts become JSON (records may be sent as a real JSON list)."""
    if value is None:
        return ""
    return json.dumps(value) if isinstance(value, (list, dict)) else str(value)


def _inv_row(r):
    return {k: r[k] for k in ("id", "code", "question", "title", "site", "priority", "status", "confidence",
                              "faults", "revisions", "created_at", "updated_at", "error", "mode", "agent",
                              "parent_id")} | {"running": _alive(r["id"])}


def _action_row(a):
    d = dict(a)
    d["evidence"] = store.loads(a["evidence_json"], [])
    d["missing_evidence"] = store.loads(a.get("missing_evidence_json"), [])
    d.pop("evidence_json", None)
    d.pop("missing_evidence_json", None)
    return d


def _start(orch):
    target = orch.run_agent if orch.mode == INDIVIDUAL_AGENT else orch.run_workflow
    t = threading.Thread(target=target, daemon=True, name=f"run-{orch.id}")
    with RUN_LOCK:
        for done in [i for i, th in RUNNING.items() if not th.is_alive()]:  # finished threads are not kept
            del RUNNING[done]
        RUNNING[orch.id] = t
        t.start()


def recover_interrupted():
    """Runs whose thread died with a previous server process would otherwise show 'Investigating' forever."""
    store.execute("UPDATE investigations SET status = 'Error', error = 'Interrupted: the server stopped while "
                  "this run was in progress. Start a new investigation.' WHERE mode = 'FULL_WORKFLOW' AND "
                  "status IN ('Detected', 'Investigating')")
    store.execute("UPDATE investigations SET status = 'Failed', error = 'Interrupted: the server stopped while "
                  "this agent was running.' WHERE mode = 'INDIVIDUAL_AGENT' AND status = 'Running'")


def health():
    comp = {"Orchestrator": "Healthy", "Strategist": "Idle", "Scout": "Idle", "Intelligence": "Idle",
            "Guardian": "Idle", "Tool Layer": "Idle", "Audit": "Healthy"}
    notes = {}
    try:
        store.one("SELECT 1 AS ok")
    except Exception as e:
        comp["Audit"], notes["Audit"] = "Down", str(e)
    live_ids = _live_ids()
    last = store.one("SELECT * FROM investigations WHERE id = ?", (max(live_ids),)) if live_ids else \
        store.one("SELECT * FROM investigations WHERE is_benchmark = 0 ORDER BY id DESC LIMIT 1")
    if live_ids:
        comp["Orchestrator"] = "Running"
    if last:
        if last["status"] in ("Error", "Failed", "Timeout"):
            comp["Orchestrator"], notes["Orchestrator"] = "Failed", last["error"]
        for role, name in (("strategist", "Strategist"), ("scout", "Scout"), ("intelligence", "Intelligence"),
                           ("guardian", "Guardian")):
            runs = store.query("SELECT status, error FROM agent_runs WHERE investigation_id = ? AND agent = ?",
                               (last["id"], role))
            if last["id"] in live_ids and ((last["mode"] == INDIVIDUAL_AGENT and last["agent"] == role) or runs):
                comp[name] = "Running"
            elif any(r["status"] == "error" for r in runs):
                comp[name] = "Failed"
            elif any(r["status"] in ("recovered", "degraded") for r in runs):
                comp[name] = "Recovered" if all(r["status"] != "degraded" for r in runs) else "Degraded"
            elif runs:
                comp[name] = "Healthy"
        outcomes = [r["outcome"] for r in store.query(
            "SELECT outcome FROM tool_executions WHERE investigation_id = ?", (last["id"],))]
        if last["id"] in live_ids and outcomes:
            comp["Tool Layer"] = "Running"
        elif "failed" in outcomes:
            comp["Tool Layer"], notes["Tool Layer"] = "Degraded", "a source failed on primary and fallback"
        elif "fallback_ok" in outcomes:
            comp["Tool Layer"], notes["Tool Layer"] = "Recovered", "fallback source used"
        elif outcomes:
            comp["Tool Layer"] = "Healthy"
    overall = "Operational" if all(v in ("Healthy", "Recovered", "Idle", "Running") for v in comp.values()) \
        else "Degraded"
    return {"overall": overall, "components": comp, "notes": notes,
            "basis": f"latest execution {last['code']}" if last else "no executions yet"}


def overview():
    invs = store.query(f"SELECT * FROM investigations i WHERE is_benchmark = 0 AND {WF} ORDER BY id DESC")
    individual = store.one("SELECT COUNT(*) AS n FROM investigations WHERE mode = ?", (INDIVIDUAL_AGENT,))["n"]
    acts = store.query("SELECT a.* FROM actions a JOIN investigations i ON i.id = a.investigation_id "
                       "WHERE i.is_benchmark = 0")
    # Workflow KPIs: individual-agent runs excluded; a successful fallback is a recovery, not a failure.
    recov = store.one("SELECT COUNT(*) AS n FROM tool_executions t JOIN investigations i ON i.id = t.investigation_id "
                      f"WHERE i.is_benchmark = 0 AND {WF} AND t.outcome = 'fallback_ok'")["n"]
    retries = store.one("SELECT COUNT(*) AS n FROM tool_executions t JOIN investigations i ON i.id = t.investigation_id "
                        f"WHERE i.is_benchmark = 0 AND {WF} AND t.outcome NOT IN ('ok', 'fallback_ok')")["n"]
    latest = store.one("SELECT * FROM investigations WHERE is_benchmark = 0 ORDER BY id DESC LIMIT 1")
    budget = store.loads(latest["budget_json"], {}) if latest else {}
    live = set(_live_ids())
    running = [_inv_row(r) for r in store.query(
        "SELECT * FROM investigations WHERE is_benchmark = 0 ORDER BY id DESC") if r["id"] in live]
    activity = store.query("SELECT a.*, i.code FROM audit_logs a JOIN investigations i ON i.id = a.investigation_id "
                           "WHERE i.is_benchmark = 0 ORDER BY a.id DESC LIMIT 14")
    return {
        "product": PRODUCT, "demo_data": True, "provider": active_provider(),
        "kpis": {"active": sum(i["status"] in ACTIVE_STATUSES for i in invs),
                 "resolved": sum(i["status"] in ("Approved", "Completed") for i in invs),
                 "pending_approval": sum(a["status"] == "Awaiting Approval" for a in acts),
                 "recovery_events": recov, "tool_failures": retries, "total": len(invs),
                 "individual_runs": individual},
        "budget": budget, "health": health(), "running_executions": running,
        "investigations": [_inv_row(i) for i in invs[:8]],
        "activity": activity,
        "failures": {k: v[0] for k, v in UI_FAILURES.items()},
    }


def actions():
    """Action Board projection shared by Kanban and table views."""
    return [_action_row(a) | {"investigation_code": a["inv_code"],
                              "investigation_title": a["inv_title"],
                              "investigation_mode": a["inv_mode"],
                              "site": a["inv_site"]}
            for a in store.query(
                "SELECT a.*, i.code AS inv_code, i.title AS inv_title, i.mode AS inv_mode, i.site AS inv_site "
                "FROM actions a JOIN investigations i ON i.id = a.investigation_id "
                "WHERE i.is_benchmark = 0 ORDER BY a.id DESC")]


def investigation(inv_id):
    inv = store.one("SELECT * FROM investigations WHERE id = ?", (inv_id,))
    if not inv:
        return None
    return _inv_row(inv) | {
        "plan": store.loads(inv["plan_json"]), "result": store.loads(inv["result_json"]),
        "guardian": store.loads(inv["guardian_json"]), "budget": store.loads(inv["budget_json"], {}),
        "actions": [_action_row(a) for a in store.query("SELECT * FROM actions WHERE investigation_id = ? ORDER BY id",
                                                        (inv_id,))],
        "audit": store.query("SELECT * FROM audit_logs WHERE investigation_id = ? ORDER BY id", (inv_id,)),
        # key kept as "tool_calls" for existing UI/API consumers; rows come from tool_executions
        "tool_calls": store.query("SELECT * FROM tool_executions WHERE investigation_id = ? ORDER BY id", (inv_id,)),
        "recommendations": store.query("SELECT * FROM recommendations WHERE investigation_id = ? ORDER BY rank",
                                       (inv_id,)),
        "approvals": store.query("SELECT p.* FROM approvals p JOIN actions a ON a.id = p.action_id "
                                 "WHERE a.investigation_id = ? ORDER BY p.id", (inv_id,)),
        "agent_runs": store.query("SELECT agent, task, status, duration_ms, llm_used, error FROM agent_runs "
                                  "WHERE investigation_id = ? ORDER BY id", (inv_id,)),
    }


def execution(ex_id):
    """Individual execution detail: output, audit, handoffs, lineage."""
    inv = store.one("SELECT * FROM investigations WHERE id = ? AND mode = ?", (ex_id, INDIVIDUAL_AGENT))
    if not inv:
        return None
    d = investigation(ex_id)
    d["context"] = store.loads(inv["context_json"], {})
    d["output"] = d.pop("result")
    if d["output"] and d["output"].get("collected"):  # full records are large; send a preview to the UI
        col = d["output"].pop("collected")
        d["output"]["preview"] = {t: c["records"][:8] for t, c in col.items()}
    d["handoffs"] = [] if d["running"] else [{"target": t, "label": HANDOFF_LABELS[t]}
                                             for t in available_handoffs(inv)]
    d["children"] = [_inv_row(c) for c in store.query("SELECT * FROM investigations WHERE parent_id = ? ORDER BY id",
                                                      (ex_id,))]
    d["parent"] = _inv_row(p) if (p := store.one("SELECT * FROM investigations WHERE id = ?",
                                                  (inv["parent_id"],))) else None
    return d


def executions(params):
    agent = params.get("agent", [""])[0]
    clause, args = ("AND agent = ?", (agent,)) if agent in AGENT_POLICY else ("", ())
    return [_inv_row(r) for r in store.query(
        f"SELECT * FROM investigations WHERE mode = ? {clause} ORDER BY id DESC LIMIT 50", (INDIVIDUAL_AGENT, *args))]


def sites():
    snap = load_snapshot()
    return {"sites": [{"code": k, "label": v["label"], "name": v["name"]} for k, v in snap["sites"].items()],
            "window": dataset_window(), "tools": list(AGENT_POLICY["scout"]["tools"]),
            "failures": {a: {k: UI_FAILURES[k][0] for k in modes if k in UI_FAILURES}  # aliases hidden
                         for a, modes in AGENT_FAILURES.items()}}


AUDIT_FILTERS = {
    "all": ("", ()),
    "strategist": ("AND a.role = ?", ("strategist",)),
    "scout": ("AND a.role = ?", ("scout",)),
    "intelligence": ("AND a.role = ?", ("intelligence",)),
    "guardian": ("AND a.role = ?", ("guardian",)),
    "errors": ("AND a.status IN ('error', 'stopped', 'rejected', 'warn')", ()),
    "retries": ("AND (a.retry_count > 0 OR a.action IN ('tool_failure', 'fallback', 'degraded'))", ()),
    "approvals": ("AND a.role = 'human'", ()),
    "workflow": ("AND a.mode = 'FULL_WORKFLOW'", ()),
    "individual": ("AND a.mode = 'INDIVIDUAL_AGENT'", ()),
}
AUDIT_ROLES = {"orchestrator", "strategist", "scout", "intelligence", "guardian", "human"}
AUDIT_STATUSES = {"ok", "warn", "recovered", "rejected", "error", "stopped"}
AUDIT_MODES = {"FULL_WORKFLOW", "INDIVIDUAL_AGENT"}


def _param(params, name):
    value = params.get(name, [""])
    return str(value[0] if isinstance(value, list) else value).strip()


def audit(params):
    """Composable Audit Trail query; fixed legacy chips remain backward compatible."""
    preset = _param(params, "filter") or "all"
    clause, base_args = AUDIT_FILTERS.get(preset, AUDIT_FILTERS["all"])
    clauses, args = [clause] if clause else [], list(base_args)

    role = _param(params, "agent")
    if role in AUDIT_ROLES:
        clauses.append("AND a.role = ?")
        args.append(role)
    status_value = _param(params, "status")
    if status_value in AUDIT_STATUSES:
        clauses.append("AND a.status = ?")
        args.append(status_value)
    mode = _param(params, "mode")
    if mode in AUDIT_MODES:
        clauses.append("AND a.mode = ?")
        args.append(mode)
    for key, column in (("investigation", "a.investigation_id"), ("execution", "a.execution_id")):
        value = _param(params, key)
        if value.isdigit():
            clauses.append(f"AND {column} = ?")
            args.append(int(value))
    event = _param(params, "event")
    if event == "failure":
        clauses.append("AND (a.status IN ('error','stopped','rejected','warn') OR "
                       "a.action IN ('tool_failure','degraded','error','stop'))")
    elif event == "recovery":
        clauses.append("AND (a.status = 'recovered' OR a.action = 'fallback' OR a.fallback_used = 1)")
    date_from, date_to = _param(params, "from"), _param(params, "to")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from):
        clauses.append("AND substr(a.timestamp, 1, 10) >= ?")
        args.append(date_from)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
        clauses.append("AND substr(a.timestamp, 1, 10) <= ?")
        args.append(date_to)
    search = _param(params, "search")
    if search:
        like = f"%{search[:100]}%"
        clauses.append("AND (a.agent LIKE ? OR a.action LIKE ? OR a.reason LIKE ? OR a.input LIKE ? OR "
                       "a.output LIKE ? OR i.code LIKE ?)")
        args.extend([like] * 6)
    where = " ".join(clauses)
    return store.query(f"SELECT a.*, i.code FROM audit_logs a JOIN investigations i ON i.id = a.investigation_id "
                       f"WHERE i.is_benchmark = 0 {where} ORDER BY a.id DESC LIMIT 400", tuple(args))


def agents():
    out = []
    desc = {"strategist": "Planning & delegation", "scout": "Data & tool execution",
            "intelligence": "Analysis & recommendations", "guardian": "Verification & quality control"}
    for role in ("strategist", "scout", "intelligence", "guardian"):
        runs = store.query("SELECT r.* FROM agent_runs r JOIN investigations i ON i.id = r.investigation_id "
                           "WHERE i.is_benchmark = 0 AND r.agent = ? ORDER BY r.id", (role,))
        n = len(runs)
        # "rejected" = Guardian did its job (returned a verdict); only errors/timeouts count as failures
        ok = sum(r["status"] in ("ok", "recovered", "rejected", "degraded") for r in runs)
        if role == "scout":
            handled = store.one("SELECT COUNT(*) AS n FROM tool_executions t JOIN investigations i ON "
                                "i.id = t.investigation_id WHERE i.is_benchmark = 0 AND t.outcome != 'ok'")["n"]
            handled_label = "tool failures handled"
        elif role == "guardian":
            handled = store.one("SELECT COUNT(*) AS n FROM audit_logs a JOIN investigations i ON "
                                "i.id = a.investigation_id WHERE i.is_benchmark = 0 AND a.role = 'guardian' "
                                "AND a.status = 'rejected'")["n"]
            handled_label = "weak results rejected"
        elif role == "intelligence":
            handled = sum(r["task"].startswith("revise") for r in runs)
            handled_label = "revisions completed"
        else:
            handled = sum(r["task"] == "plan" for r in runs if r["llm_used"])
            handled_label = "plans from LLM (rest rule-based)"
        mode_n = Counter(r["mode"] for r in store.query(
            "SELECT i.mode FROM agent_runs r JOIN investigations i ON i.id = r.investigation_id "
            "WHERE i.is_benchmark = 0 AND r.agent = ?", (role,)))
        live = _live_ids()
        current = None
        for inv_id in live:
            inv = store.one("SELECT code, mode, agent, question FROM investigations WHERE id = ?", (inv_id,))
            last = store.one("SELECT role, action FROM audit_logs WHERE investigation_id = ? ORDER BY id DESC LIMIT 1",
                             (inv_id,))
            if inv and ((inv["mode"] == INDIVIDUAL_AGENT and inv["agent"] == role) or (last and last["role"] == role)):
                current = f"{inv['code']}: {inv['question'][:60]}"
        out.append({"role": role, "name": AGENT_NAMES[role], "description": desc[role], "tasks": n,
                    "policy": AGENT_POLICY[role], "current_task": current,
                    "by_mode": {"workflow": mode_n.get("FULL_WORKFLOW", 0),
                                "individual": mode_n.get(INDIVIDUAL_AGENT, 0)},
                    "timeouts": sum(r["status"] == "timeout" for r in runs),
                    "success_rate": round(ok / n * 100) if n else None,
                    "avg_ms": round(sum(r["duration_ms"] for r in runs) / n) if n else None,
                    "handled": handled, "handled_label": handled_label,
                    "llm_calls": sum(r["llm_used"] for r in runs),
                    "status": "Running" if current else
                    "Failed" if runs and runs[-1]["status"] in ("error", "timeout") else "Healthy" if n else "Idle"})
    return out


def analytics():
    invs = store.query(f"SELECT * FROM investigations i WHERE is_benchmark = 0 AND {WF}")
    indiv = store.query("SELECT agent, status FROM investigations WHERE mode = ?", (INDIVIDUAL_AGENT,))
    by_status = Counter(i["status"] for i in invs)
    sev = {}
    for e in store.query("SELECT e.record_id, e.payload_json FROM events e JOIN investigations i ON "
                         "i.id = e.investigation_id WHERE i.is_benchmark = 0 AND e.kind = 'incident'"):
        sev[e["record_id"]] = store.loads(e["payload_json"], {}).get("severity", "?")
    agent_ms = {r["agent"]: round(r["avg"]) for r in store.query(
        "SELECT r.agent, AVG(r.duration_ms) AS avg FROM agent_runs r JOIN investigations i ON "
        "i.id = r.investigation_id WHERE i.is_benchmark = 0 GROUP BY r.agent")}
    tools = defaultdict(lambda: {"ok": 0, "failures": 0, "recovered": 0, "failed": 0})
    for t in store.query("SELECT t.tool, t.outcome FROM tool_executions t JOIN investigations i ON "
                         "i.id = t.investigation_id WHERE i.is_benchmark = 0"):
        name = t["tool"].split(":")[0]
        key = {"ok": "ok", "fallback_ok": "recovered", "failed": "failed"}.get(t["outcome"], "failures")
        tools[name][key] += 1
    # Workflow Guardian verdicts only: individual reviews of deliberately bad claims would inflate the rate.
    verdicts = [a["status"] for a in store.query(
        "SELECT a.status FROM audit_logs a JOIN investigations i ON i.id = a.investigation_id "
        f"WHERE i.is_benchmark = 0 AND {WF} AND a.role = 'guardian' AND a.action = 'verify'")]
    finished = [i for i in invs if i["status"] not in ("Detected", "Investigating")]
    bench = store.query("SELECT status FROM investigations WHERE is_benchmark = 1")
    latest_bench = store.one("SELECT * FROM benchmark_runs ORDER BY id DESC LIMIT 1")
    if latest_bench:
        latest_bench["results"] = store.loads(latest_bench.pop("results_json"), [])
    bench_invocations = store.one("SELECT COUNT(*) AS n FROM benchmark_runs")["n"]
    tool_failures = sum(v["failures"] + v["failed"] for v in tools.values())
    recovery_count = sum(v["recovered"] for v in tools.values())
    # Portfolio roll-up of the risk / opportunity fields the Intelligence agent already generates per action.
    # These are real generated values (not fabricated); we only group identical statements and count them.
    risks, opps = defaultdict(lambda: {"count": 0, "codes": set(), "priorities": Counter()}), \
        defaultdict(lambda: {"count": 0, "codes": set()})
    for a in store.query("SELECT a.risk, a.opportunity, a.priority, i.code FROM actions a JOIN investigations i "
                         f"ON i.id = a.investigation_id WHERE i.is_benchmark = 0 AND {WF}"):
        if a["risk"]:
            r = risks[a["risk"].strip()]
            r["count"] += 1; r["codes"].add(a["code"]); r["priorities"][a["priority"] or "Low"] += 1
        if a["opportunity"]:
            o = opps[a["opportunity"].strip()]
            o["count"] += 1; o["codes"].add(a["code"])
    top_priority = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    op_risks = sorted(
        [{"risk": k, "count": v["count"], "investigations": sorted(v["codes"]),
          "top_priority": min(v["priorities"], key=lambda p: top_priority.get(p, 9), default="Low")}
         for k, v in risks.items()],
        key=lambda x: (top_priority.get(x["top_priority"], 9), -x["count"]))
    opportunities = sorted(
        [{"opportunity": k, "count": v["count"], "investigations": sorted(v["codes"])} for k, v in opps.items()],
        key=lambda x: -x["count"])
    return {
        "investigations_by_status": dict(by_status),
        "individual_by_status": dict(Counter(r["status"] for r in indiv)),
        "individual_by_agent": {AGENT_NAMES.get(k, k): v for k, v in Counter(r["agent"] for r in indiv).items()},
        "incident_priority": dict(Counter(sev.values())),
        "agent_ms": {AGENT_NAMES.get(k, k): v for k, v in agent_ms.items()},
        "tools": dict(tools), "tool_failures": tool_failures, "recovery_count": recovery_count,
        "operational_risks": op_risks, "business_opportunities": opportunities,
        "guardian": {"reviews": len(verdicts), "rejected": verdicts.count("rejected"),
                     "rate": round(verdicts.count("rejected") / len(verdicts) * 100) if verdicts else None},
        "completion": {"finished": len(finished), "completed": sum(i["status"] in FINAL for i in finished),
                       "rate": round(sum(i["status"] in FINAL for i in finished) / len(finished) * 100)
                       if finished else None},
        "benchmark": {"runs": len(bench), "completed": sum(b["status"] in FINAL for b in bench),
                      "invocations": bench_invocations, "latest": latest_bench,
                      "methodology": (latest_bench or {}).get("methodology") or
                      "Simulated benchmark data only; run python benchmark.py for the deterministic acceptance matrix."},
    }


def incidents():
    """Incident catalog from SQLite (seeded from the Ops snapshot, refreshed whenever Scout collects)."""
    sites = load_snapshot()["sites"]
    linked = defaultdict(list)
    for e in store.query("SELECT DISTINCT e.record_id, i.id, i.code, i.mode FROM events e JOIN investigations i "
                         "ON i.id = e.investigation_id WHERE e.kind = 'incident' AND i.is_benchmark = 0 "
                         "ORDER BY i.id"):
        linked[e["record_id"]].append({"id": e["id"], "code": e["code"], "mode": e["mode"]})
    out = []
    for r in store.query("SELECT * FROM incidents ORDER BY opened_at, incident_id"):
        payload = store.loads(r["payload_json"], {})
        out.append({"id": r["incident_id"], "site": r["site"], "time": r["opened_at"],
                    "opened": payload.get("opened", r["opened_at"]), "severity": r["severity"],
                    "category": r["category"], "summary": r["summary"], "source": r["source"],
                    "times_collected": r["times_collected"], "investigations": linked.get(r["incident_id"], []),
                    "site_label": sites.get(r["site"], {}).get("label", r["site"])})
    return out


def approvals():
    """Human Approval history (newest first)."""
    return store.query("SELECT p.*, a.code AS action_code, a.recommended, i.code AS investigation_code "
                       "FROM approvals p LEFT JOIN actions a ON a.id = p.action_id "
                       "LEFT JOIN investigations i ON i.id = COALESCE(p.investigation_id, a.investigation_id) "
                       "ORDER BY p.id DESC LIMIT 200")


def benchmarks():
    return [r | {"results": store.loads(r.pop("results_json"), [])}
            for r in store.query("SELECT * FROM benchmark_runs ORDER BY id DESC LIMIT 20")]


def settings():
    return {"provider": active_provider(), "limits": {
        "max_workflow_steps": MAX_WORKFLOW_STEPS, "execution_timeout": EXECUTION_TIMEOUT,
        "max_guardian_revisions": MAX_GUARDIAN_REVISIONS, "max_tool_retries": MAX_TOOL_RETRIES,
        "max_steps": MAX_STEPS, "max_seconds": MAX_SECONDS, "max_llm_calls": MAX_LLM_CALLS,
        "max_revisions": MAX_REVISIONS, "scout_attempts": SCOUT_MAX_ATTEMPTS,
        "scout_timeout_s": SCOUT_TIMEOUT_SECONDS, "thermal_threshold_c": THERMAL_THRESHOLD_C,
        "demo_step_delay_s": DEMO_STEP_DELAY_SECONDS}, "bind": f"{HOST}:{PORT}"}


# ---------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "VelloeOps/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; the audit trail is the log

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0:  # rfile.read(-1) would block until the client closes the socket
            raise ValueError("invalid Content-Length")
        if n > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except RecursionError:  # deeply nested JSON
            raise ValueError("request body is nested too deeply")
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def do_GET(self):
        url = urlparse(self.path)
        path, params = url.path, parse_qs(url.query)
        try:
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(WEB, name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            routes = {"/api/overview": overview, "/api/agents": agents, "/api/analytics": analytics,
                      "/api/incidents": incidents, "/api/settings": settings,
                      "/api/investigations": lambda: [_inv_row(i) for i in store.query(
                          f"SELECT * FROM investigations i WHERE is_benchmark = 0 AND {WF} ORDER BY id DESC")],
                      "/api/sites": sites,
                      "/api/actions": actions, "/api/approvals": approvals, "/api/benchmarks": benchmarks,
                      "/api/database": store.info}
            if path in routes:
                return self._send(200, routes[path]())
            if path == "/api/audit":
                return self._send(200, audit(params))
            if path == "/api/executions":
                return self._send(200, executions(params))
            m = re.fullmatch(r"/api/executions/(\d{1,12})", path)
            if m:
                ex = execution(int(m.group(1)))
                return self._send(200, ex) if ex else self._send(404, {"error": "execution not found"})
            m = re.fullmatch(r"/api/investigations/(\d{1,12})", path)
            if m:  # individual executions have their own endpoint and page shape
                inv = investigation(int(m.group(1)))
                if not inv or inv["mode"] == INDIVIDUAL_AGENT:
                    return self._send(404, {"error": "investigation not found"})
                return self._send(200, inv)
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/investigations":
                q = _text(body.get("question")).strip()
                failure = _text(body.get("failure")) or "normal"
                failure = UI_FAILURE_ALIASES.get(failure, failure)
                if not q or len(q) > 500:
                    return self._send(400, {"error": "question must be 1-500 characters"})
                if failure not in UI_FAILURES:
                    return self._send(400, {"error": "unknown failure mode"})
                orch = Orchestrator(q, failure=failure, pace=DEMO_STEP_DELAY_SECONDS)
                _start(orch)
                return self._send(201, {"id": orch.id, "code": orch._code()})
            m = re.fullmatch(r"/api/agents/(strategist|scout|intelligence|guardian)/run", path)
            if m:  # INDIVIDUAL_AGENT mode - validation happens in the orchestrator and is audited
                task = _text(body.get("task")).strip()
                raw_ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
                ctx = {k: _text(raw_ctx[k]) for k in CONTEXT_KEYS if _text(raw_ctx.get(k)).strip()}
                orch = Orchestrator(task, failure=_text(body.get("failure")) or "none", pace=DEMO_STEP_DELAY_SECONDS,
                                    mode=INDIVIDUAL_AGENT, agent=m.group(1), context=ctx)
                _start(orch)
                return self._send(201, {"id": orch.id, "code": orch.code})
            m = re.fullmatch(r"/api/executions/(\d{1,12})/handoff", path)
            if m:
                ex_id = int(m.group(1))
                src = store.one("SELECT mode FROM investigations WHERE id = ?", (ex_id,))
                if not src or src["mode"] != INDIVIDUAL_AGENT:
                    return self._send(404, {"error": "execution not found"})
                with HANDOFF_LOCK:  # check-then-create is atomic across request threads
                    if _alive(ex_id):
                        return self._send(409, {"error": "execution is still running"})
                    res = prepare_handoff(ex_id, _text(body.get("target")), failure=_text(body.get("failure")) or "none",
                                          pace=DEMO_STEP_DELAY_SECONDS)
                    if isinstance(res, list):  # "Create Recommended Action"
                        return self._send(201, {"actions": [_action_row(a) for a in res]})
                    _start(res)
                return self._send(201, {"id": res.id, "code": res.code, "agent": res.agent})
            m = re.fullmatch(r"/api/investigations/(\d{1,12})/more-evidence", path)
            if m:
                inv_id = int(m.group(1))
                inv = store.one("SELECT * FROM investigations WHERE id = ?", (inv_id,))
                if not inv or inv["mode"] == INDIVIDUAL_AGENT:
                    return self._send(404, {"error": "investigation not found"})
                with HANDOFF_LOCK:  # two quick clicks must not start two re-runs of the same investigation
                    if _alive(inv_id):
                        return self._send(409, {"error": "investigation is already running"})
                    blocker = rerun_blocker(inv_id)
                    if blocker:
                        return self._send(409, {"error": blocker})
                    orch = Orchestrator(inv["question"], failure="none", pace=DEMO_STEP_DELAY_SECONDS,
                                        investigation_id=inv_id, more_evidence=True)
                    _start(orch)
                return self._send(202, {"id": inv_id})
            m = re.fullmatch(r"/api/actions/(\d+)/(approve|reject|complete)", path)
            if m:
                aid, verb = int(m.group(1)), m.group(2)
                note = _text(body.get("note"))[:300]
                a = complete_action(aid) if verb == "complete" else decide_action(aid, verb, note=note)
                return self._send(200, _action_row(a))
            if path == "/api/reset":
                if body.get("confirm") != "RESET":
                    return self._send(400, {"error": "confirmation required"})
                with HANDOFF_LOCK:  # no new run can start between the check and the reset
                    if _live_ids():
                        return self._send(409, {"error": "wait for running investigations to finish"})
                    store.reset()  # clears stored runs; the incident catalog is re-seeded
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except KeyError as e:
            self._send(404, {"error": str(e).strip("'")})
        except ValueError as e:  # state conflicts (already decided, created twice...) are 409, bad input 400
            self._send(409 if CONFLICT.search(str(e)) else 400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})


def main():
    store.init()
    recover_interrupted()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"{PRODUCT} dashboard: http://{HOST}:{PORT}  (LLM provider: {active_provider()}; Ctrl+C to stop)")
    if HOST not in ("127.0.0.1", "localhost"):
        print("WARNING: no authentication - bound to a non-local address.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
