"""
Orchestrator - the system control layer (NOT an AI agent).

Coordinates the four agents, owns shared state (Blackboard), executes the plan,
applies fault simulation, enforces step/time/LLM-call and revision limits, writes
the audit trail and persists everything to SQLite. Human approval of actions is
handled here too (approve_action / complete_action), never by an agent.

Workflow: DETECT -> PLAN -> COLLECT -> ANALYZE/INVESTIGATE -> VERIFY (bounded
revision) -> RECOMMEND -> HUMAN APPROVAL -> ACTION -> AUDIT

Two execution modes share this one control layer:
  FULL_WORKFLOW     run_workflow(): all four agents, automatic delegation (primary mode)
  INDIVIDUAL_AGENT  run_agent(): one selected agent, same limits/timeouts/tool policy/audit;
                    further agents only run through explicit, user-initiated handoffs.
"""
import re
import threading
import time
import traceback
from datetime import datetime

import store
from agents import guardian, intelligence, strategist
from agents import scout as scout_mod
from agents.scout import Scout
from blackboard import FULL_WORKFLOW, INDIVIDUAL_AGENT, Blackboard
from config import (AGENT_NAMES, AGENT_TIMEOUT_SECONDS, MAX_LLM_CALLS, MAX_REVISIONS, MAX_SECONDS,
                    MAX_STEPS)
from tools import faults as fault_mod
from tools.ops_api import dataset_window, site_registry

ACTIVE_STATUSES = ("Detected", "Investigating", "Awaiting Approval", "Needs Review")
MAX_TASK_CHARS = 4000
MAX_CONTEXT_CHARS = 20000

# Tool permissions and LLM use per agent. Enforced for every execution in both modes.
AGENT_POLICY = {
    "strategist": {"tools": (), "llm": True, "purpose": "plan only - never collects data"},
    "scout": {"tools": ("incidents", "telemetry", "maintenance", "weather"), "llm": False,
              "purpose": "collect, validate and normalize data"},
    "intelligence": {"tools": (), "llm": True, "purpose": "analyze supplied or collected records"},
    "guardian": {"tools": (), "llm": False, "purpose": "verify; never revises automatically"},
}
HANDOFF_LABELS = {"scout": "Send Plan to Scout", "intelligence": "Analyze with Intelligence",
                  "guardian": "Verify with Guardian", "revise": "Send for Revision",
                  "action": "Create Recommended Action"}


class Stop(Exception):
    pass


class ValidationFailed(ValueError):
    pass


class RerunRefused(Exception):
    pass


def rerun_blocker(investigation_id):
    """Reason a workflow may not be re-run (Request More Evidence), or None. Re-running replaces the
    derived analysis and actions, so it is refused once a human has approved, rejected or completed one."""
    decided = store.one("SELECT COUNT(*) AS n FROM actions WHERE investigation_id = ? AND "
                        "(approval_state IN ('approved', 'rejected') OR status = 'Completed')", (investigation_id,))
    if decided and decided["n"]:
        return (f"{decided['n']} action(s) already have a human decision; start a new investigation instead "
                "of re-running this one")
    return None


class AgentTimeout(Exception):
    pass


class Orchestrator:
    def __init__(self, question, failure="none", fault_spec=None, fault_rate=0.0, seed=None,
                 pace=0.0, is_benchmark=False, investigation_id=None, more_evidence=False,
                 mode=FULL_WORKFLOW, agent=None, context=None, parent_id=None, benchmark_run_id=None):
        self.question = (question or "").strip()
        self.failure, self.fault_spec, self.fault_rate, self.seed = failure, fault_spec, fault_rate, seed
        self.more_evidence = more_evidence
        self.mode, self.agent, self.context, self.parent_id = mode, agent, dict(context or {}), parent_id
        store.init()
        if investigation_id is None:
            individual = mode == INDIVIDUAL_AGENT
            investigation_id = store.insert(
                "investigations", question=self.question,
                title=f"{AGENT_NAMES.get(agent, agent)}: {self.question[:70]}" if individual else "New investigation",
                status="Running" if individual else "Detected", is_benchmark=int(is_benchmark), mode=mode,
                agent=agent, parent_id=parent_id, context_json=store.dumps(self.context) if individual else None,
                faults=failure if individual else None, benchmark_run_id=benchmark_run_id,
                created_at=store.now(), updated_at=store.now())
            prefix = "EX" if individual else "VX"
            store.update("investigations", investigation_id, code=f"{prefix}-{investigation_id:05d}")
        self.id = investigation_id
        self.code = store.one("SELECT code FROM investigations WHERE id = ?", (self.id,))["code"]
        self.bb = Blackboard(self.id, self.question, MAX_STEPS, MAX_SECONDS, MAX_LLM_CALLS, pace=pace, mode=mode)

    # ------------------------------------------------------------------ helpers
    def _set(self, **fields):
        store.update("investigations", self.id, updated_at=store.now(), **fields)

    def _budget(self, where):
        reason = self.bb.budget_exceeded()
        if reason:
            explanation = f"STOPPED SAFELY: {reason}; partial results retained"
            self.bb.log("orchestrator", "stop", where, explanation,
                        reason="Stopping conditions were reached; no further agents or tools were started.",
                        status="stopped")
            raise Stop(explanation)

    def _collect(self, task, scout, plan, collected):
        self._budget(f"before collecting {task['tool']}")
        self.bb.update_state(execution_status="COLLECT", plan=plan)
        with self.bb.agent_run("scout", f"collect {task['tool']}") as run:
            res = scout.collect(task, plan)
            run["status"] = "ok" if res["status"] == "ok" else "recovered" if res["status"] == "degraded_fallback" \
                else "degraded"
        collected[task["tool"]] = res
        res["selection_reason"] = task.get("reason") or "The source was explicitly selected for this task."
        if self.bb.late():  # the orchestrator already timed this worker out: persist nothing more
            collected[task["tool"]] = res
            return
        for r in res["records"]:
            store.insert("events", investigation_id=self.id, record_id=r["id"], kind=r["type"], site=r.get("site"),
                         time=r.get("time") or r.get("start") or r.get("date"), source=res["source"],
                         payload_json=store.dumps(r))
            if r["type"] == "incident":  # durable incident catalog, linked back via events.record_id
                store.upsert_incident(r, investigation_id=self.id, source=res["source"])
        records = [record for source in collected.values() for record in source["records"]]
        self.bb.update_state(collected=collected, events=records,
                             evidence=sorted({record["id"] for record in records}))
        self._budget(f"after collecting {task['tool']}")

    def _faults(self, plan):
        if self.fault_spec is not None:
            spec = self.fault_spec
        else:
            first = next((t["tool"] for t in (plan or {}).get("tasks", [])), "incidents")
            spec = fault_mod.ui_failure_spec(self.failure or "none", first)
        return fault_mod.build(spec, rate=self.fault_rate, seed=self.seed), spec

    def _log_analysis(self, result, meta, feedback, n_records):
        """Audit entries for one Intelligence pass (shared by both execution modes)."""
        bb, top = self.bb, (result["hypotheses"][0] if result["hypotheses"] else None)
        bb.log("intelligence", "detect_patterns", f"{n_records} records", [p["statement"] for p in result["patterns"]],
               reason="Anomalies and correlations computed deterministically from the records.")
        bb.log("intelligence", "rank_hypotheses", f"{len(result['hypotheses'])} hypotheses",
               [f"{h['title']} ({h['confidence']}%)" for h in result["hypotheses"]],
               reason=f"Leading: {top['title']} at {top['confidence']}% ({top['label']})" if top
               else "No hypothesis could be formed from the available data.")
        bb.log("intelligence", "recommend_actions", "action & opportunity engine",
               [f"[{a['priority']}] {a['recommended']}" for a in result["actions"]],
               reason=("Revision addressing: " + feedback) if feedback else
               "Actions derived from the leading hypothesis, risks and data gaps."
               + (f" (simulated LLM fault: {meta['fault_injected']})" if meta.get("fault_injected") else ""),
               status="warn" if meta.get("fault_injected") else "ok")

    # ------------------------------------------------------------------ run
    def run_workflow(self):
        """FULL_WORKFLOW: Strategist -> Scout -> Intelligence -> Guardian (bounded revision) -> approval gate."""
        return self.run()

    def run(self):
        try:
            self._run()
        except RerunRefused as r:  # the investigation keeps its status, result and decisions untouched
            self.bb.log("orchestrator", "rerun_refused", self._code(), str(r),
                        reason="Human decisions are never overwritten by a re-run.", status="warn")
        except Stop as s:
            self.bb.update_state(execution_status="Stopped")
            self._set(status="Stopped", error=str(s), budget_json=store.dumps(self.bb.summary()))
        except Exception as e:  # last line of defence: never leave a run hanging
            self.bb.log("orchestrator", "error", "run", str(e), reason=traceback.format_exc()[-400:], status="error")
            self.bb.update_state(execution_status="Error")
            self._set(status="Error", error=str(e), budget_json=store.dumps(self.bb.summary()))
        return self.summary()

    def _run(self):
        bb = self.bb
        bb.update_state(execution_status="DETECT")
        # Re-run (Request More Evidence): never destroy human decisions. Refuse if any action was decided,
        # otherwise clear the previous derived analysis and stale result fields before starting again.
        blocker = rerun_blocker(self.id)
        if blocker:
            raise RerunRefused(blocker)
        for table in ("events", "evidence", "hypotheses", "actions", "recommendations"):
            store.execute(f"DELETE FROM {table} WHERE investigation_id = ?", (self.id,))
        self._set(result_json=None, guardian_json=None, error=None, priority=None, confidence=None)
        bb.log("orchestrator", "detect", self.question, f"investigation {self._code()} opened (mode {FULL_WORKFLOW})",
               reason="New request received; Full Investigation Mode - handing to Velloe Strategist for planning.")

        # PLAN ----------------------------------------------------------
        self._budget("before planning")
        with bb.agent_run("strategist", "plan") as run:
            plan, meta = strategist.plan(self.question)
            run["llm_used"] = not meta.get("mock")
        bb.note_llm(meta)
        self._budget("after planning")
        if self.more_evidence:
            strategist.add_tasks(plan, list(strategist.TOOLS))
        bb.update_state(plan=plan, execution_status="PLAN")
        bb.log("strategist", "create_plan", self.question,
               {"domain": plan["domain"], "sites": plan["site_labels"], "window": plan["window"],
                "tasks": [f"{t['id']} {t['tool']}" for t in plan["tasks"]]},
               reason=f"Planned by {plan['planned_by']}: domain '{plan['domain']}' -> "
                      + "; ".join(f"{t['tool']}: {t['reason']}" for t in plan["tasks"]))
        faults, spec = self._faults(plan)
        self._set(status="Investigating", plan_json=store.dumps(plan), faults=spec or "none",
                  site=", ".join(plan["site_labels"]))
        if spec:
            bb.log("orchestrator", "simulation", "failure simulation enabled", spec,
                   reason="Injected faults to demonstrate recovery (demo control).", status="warn")

        # COLLECT ---------------------------------------------------------
        scout, collected = Scout(bb, faults), {}
        for task in plan["tasks"]:
            self._collect(task, scout, plan, collected)

        # ANALYZE -> VERIFY (bounded revision) ------------------------------
        revision, feedback, verdict, result, summary = 0, None, None, None, ""
        while True:
            bb.current_revision = revision
            self._budget(f"analysis revision {revision}")
            bb.update_state(execution_status="ANALYZE" if revision == 0 else "INVESTIGATE")
            with bb.agent_run("intelligence", "analyze" if revision == 0 else f"revise #{revision}") as run:
                result, evidence_ids = intelligence.analyze(plan, collected)
                summary, meta = intelligence.write_summary(result, feedback=feedback, faults=faults)
                run["llm_used"] = not meta.get("mock")
            bb.note_llm(meta)
            bb.update_state(result=result, facts=result["facts"], hypotheses=result["hypotheses"],
                            recommendations=result["actions"], evidence=sorted(evidence_ids))
            self._log_analysis(result, meta, feedback, sum(len(c["records"]) for c in collected.values()))

            bb.update_state(execution_status="VERIFY")
            with bb.agent_run("guardian", "verify") as run:
                verdict = guardian.review(result, summary, evidence_ids, collected)
                run["status"] = "ok" if verdict["verdict"] == "APPROVED" else "rejected"
            bb.update_state(guardian_verdict=verdict)
            bb.log("guardian", "verify", f"revision {revision}", verdict["verdict"],
                   reason=verdict["reason"],
                   status="ok" if verdict["verdict"] == "APPROVED" else "rejected")
            if verdict["verdict"] == "APPROVED" or revision >= MAX_REVISIONS:
                break
            revision += 1
            bb.current_revision = revision
            feedback = "; ".join(verdict["reasons"])
            if verdict["evidence_requests"]:
                new = strategist.add_tasks(plan, verdict["evidence_requests"])
                bb.log("strategist", "delegate_more_evidence", verdict["evidence_requests"],
                       [f"{t['id']} {t['tool']}" for t in new],
                       reason="Guardian asked for evidence the system can still obtain; delegating to Scout.")
                for task in new:
                    self._collect(task, scout, plan, collected)
            bb.log("orchestrator", "revision", f"cycle {revision}/{MAX_REVISIONS}", "re-running Intelligence",
                   reason="Bounded revision triggered by Guardian rejection.", status="warn")

        # RECOMMEND + persist ---------------------------------------------
        approved = verdict["verdict"] == "APPROVED"
        needs_human = approved and any(a["requires_approval"] for a in result["actions"])
        status = "Awaiting Approval" if needs_human else "Approved" if approved else "Needs Review"
        result["summary"] = summary
        self._persist_result(result, verdict, revision)
        # Status is written right after the actions exist, then recomputed: an approval made in between
        # (its own refresh saw 'Investigating' and returned early) is reflected instead of overwritten.
        self._set(status=status, title=result["title"], site=result["site"] or ", ".join(plan["site_labels"]),
                  priority=result["priority"], confidence=result["confidence"], revisions=revision, error=None,
                  plan_json=store.dumps(plan), result_json=store.dumps(result),
                  guardian_json=store.dumps(verdict), budget_json=store.dumps(bb.summary()))
        _refresh_investigation(self.id)
        bb.update_state(result=result, facts=result["facts"], hypotheses=result["hypotheses"],
                        recommendations=result["actions"], guardian_verdict=verdict,
                        execution_status=status)
        bb.log("orchestrator", "complete", self._code(), status,
               reason=("Guardian approved; operational actions wait for a human." if needs_human else
                       "Guardian approved." if approved else
                       f"Guardian still rejects after {MAX_REVISIONS} revisions; flagged for human review."),
               status="ok" if approved else "rejected")
        self._set(budget_json=store.dumps(bb.summary()))

    def _persist_result(self, result, verdict, revision):
        """Full workflow: analysis rows + Action Board rows. Only Guardian-approved gated actions await a human."""
        approved = verdict["verdict"] == "APPROVED"
        rec_ids = persist_analysis(self.id, result, verdict=verdict["verdict"], verified_by=self.id,
                                   revision=revision)
        for n, a in enumerate(result["actions"], 1):
            status = ("Awaiting Approval" if a["requires_approval"] else "Detected") if approved else "Investigating"
            # Guardian-rejected actions can never be decided, so they are not "pending" approval.
            _insert_action(store, self.id, f"{self._code()}-A{n}", a, status, rec_ids.get(n),
                           approval_state=None if approved else "not_verified")

    def _code(self):
        return self.code

    def summary(self):
        inv = store.one("SELECT * FROM investigations WHERE id = ?", (self.id,))
        return {"id": self.id, "code": inv["code"], "status": inv["status"], "title": inv["title"],
                "priority": inv["priority"], "confidence": inv["confidence"], "revisions": inv["revisions"],
                "mode": inv["mode"], "agent": inv["agent"], "budget": self.bb.summary()}

    # ================================================================== INDIVIDUAL_AGENT mode
    def run_agent(self):
        """Run ONE agent through the same control layer: validation, tool policy, hard deadline,
        budget, audit, error handling and graceful degradation. Never invokes another agent."""
        bb, agent = self.bb, self.agent
        name = AGENT_NAMES.get(agent, agent)
        policy = AGENT_POLICY.get(agent, {})
        t0 = time.time()
        bb.update_state(execution_status="RUNNING")
        parent = self.context.get("source_execution")
        bb.log("orchestrator", "execution_start", {"agent": name, "task": self.question[:300]},
               f"{self.code} · mode {INDIVIDUAL_AGENT}",
               reason=f"Individual Agent Mode: only {name} runs"
                      + (f" (user handoff from {_code_of(parent)})" if parent else "") + ". "
                      f"Policy: {policy.get('purpose', 'unknown agent')}; allowed tools: "
                      f"{', '.join(policy.get('tools', ())) or 'none'}; deadline {AGENT_TIMEOUT_SECONDS:g}s.")
        output, verdict, status, error = None, None, "Failed", None
        try:
            self._validate()
            output, status = self._run_with_deadline()
            verdict = output.get("verdict") if output else None
        except AgentTimeout:
            status, error = "Timeout", f"agent exceeded the {AGENT_TIMEOUT_SECONDS:g}s execution deadline"
            store.insert("agent_runs", investigation_id=self.id, agent=agent, task="individual (timeout)",
                         status="timeout", duration_ms=int((time.time() - t0) * 1000), llm_used=0, error=error,
                         started_at=store.now())
            bb.log("orchestrator", "timeout", name, error, reason="Deadline enforced by the orchestrator; partial "
                   "results discarded, nothing fabricated.", status="stopped")
        except (ValidationFailed, intelligence.InputError) as e:
            status, error = "Validation Failed", str(e)
            bb.log("orchestrator", "validation_failed", self.question[:200], error,
                   reason="Input rejected before the agent ran.", status="error")
        except Stop as s:
            status, error = "Budget Reached", f"budget exceeded: {s}"
        except Exception as e:
            status, error = "Failed", str(e)
            bb.log("orchestrator", "error", name, error, reason=traceback.format_exc()[-400:], status="error")
        bb.log("orchestrator", "execution_complete", name, status,
               reason=error or "Result returned to the user. Further agents run only on an explicit user handoff.",
               status={"Completed": "ok", "Degraded": "recovered", "Insufficient Data": "warn"}.get(status, "error"),
               duration_ms=int((time.time() - t0) * 1000))
        bb.update_state(execution_status=status)
        result = output.get("result") if output else None
        if status == "Completed" and output:
            self._persist_individual(output)
        self._set(status=status, error=error, result_json=store.dumps(output) if output else None,
                  guardian_json=store.dumps(verdict) if verdict else None,
                  budget_json=store.dumps(bb.summary()),
                  plan_json=store.dumps(output["plan"]) if output and output.get("plan") else None,
                  priority=result.get("priority") if result else None,
                  confidence=result.get("confidence") if result else None,
                  site=(result or {}).get("site") or (", ".join(output["plan"].get("site_labels", []))
                                                      if output and output.get("plan") else None))
        return self.summary()

    def _run_with_deadline(self):
        """Run the agent body in its own thread (no shared pool, so no queueing counts against the deadline).
        On timeout the thread is marked abandoned: Blackboard drops its later writes and marks its audit rows."""
        box = {}

        def work():
            try:
                box["out"] = self._agent_body()
            except BaseException as e:  # re-raised on the caller thread
                box["err"] = e

        worker = threading.Thread(target=work, daemon=True, name=f"agent-{self.id}")
        worker.start()
        worker.join(AGENT_TIMEOUT_SECONDS)
        if worker.is_alive():
            self.bb.abandon(worker.ident)
            raise AgentTimeout()
        if "err" in box:
            raise box["err"]
        return box["out"]

    def _persist_individual(self, output):
        """Durable rows for individual results (runs on the caller thread, never after a timeout).
        Intelligence -> hypotheses/evidence/recommendations; Guardian -> verdict on the reviewed recommendations."""
        if output.get("type") == "analysis":
            persist_analysis(self.id, output["result"], revision=1 if output.get("revision_of") else 0)
        elif output.get("type") == "verdict" and output["reviewed"].get("kind") == "intelligence_result":
            store.execute("UPDATE recommendations SET guardian_verdict = ?, verified_by_id = ?, updated_at = ? "
                          "WHERE investigation_id = ?", (output["verdict"]["verdict"], self.id, store.now(),
                                                         output["reviewed"]["execution"]))

    # ------------------------------------------------------------------ validation / policy
    def _validate(self):
        c, agent = self.context, self.agent
        if agent not in AGENT_POLICY:
            raise ValidationFailed(f"unknown agent '{agent}'")
        if self.failure not in fault_mod.AGENT_FAILURES[agent]:
            raise ValidationFailed(f"simulation '{self.failure}' is not available for {AGENT_NAMES[agent]}")
        if len(self.question) > MAX_TASK_CHARS:
            raise ValidationFailed(f"task is longer than {MAX_TASK_CHARS} characters")
        if len(store.dumps(c)) > MAX_CONTEXT_CHARS:
            raise ValidationFailed(f"context data is larger than {MAX_CONTEXT_CHARS} characters")
        src = c.get("source_execution")
        if src is not None:
            parent = store.one("SELECT * FROM investigations WHERE id = ?", (src,))
            expected = {"scout": "strategist", "intelligence": "scout", "guardian": "intelligence"}.get(agent)
            if not parent or parent["mode"] != INDIVIDUAL_AGENT or parent["agent"] != expected \
                    or not parent["result_json"]:
                raise ValidationFailed(f"source execution {src} is not a finished {expected} result")
        if agent in ("strategist", "scout") and not self.question and src is None:
            raise ValidationFailed("a task / data request is required")
        if agent == "intelligence" and src is None and not str(c.get("records") or "").strip():
            raise ValidationFailed("operational records are required (one timestamped record per line)")
        if agent == "guardian" and src is None and not str(c.get("report") or self.question).strip():
            raise ValidationFailed("a report or conclusion to verify is required")
        if agent == "scout":
            if c.get("preferred_source") and c["preferred_source"] not in AGENT_POLICY["scout"]["tools"]:
                raise ValidationFailed(f"tool '{c['preferred_source']}' is not permitted for Velloe Scout")
            if c.get("site") and c["site"] not in site_registry():
                raise ValidationFailed(f"unknown site '{c['site']}'")
            for k in ("start", "end"):
                if c.get(k) and not _is_date(c[k]):
                    raise ValidationFailed(f"{k} must be YYYY-MM-DD")
            if c.get("start") and c.get("end") and c["start"] > c["end"]:
                raise ValidationFailed("start date is after end date")

    def _source(self):
        src = self.context.get("source_execution")
        row = store.one("SELECT * FROM investigations WHERE id = ?", (src,)) if src is not None else None
        return row, (store.loads(row["result_json"], {}) if row else None)

    # ------------------------------------------------------------------ agent bodies
    def _agent_body(self):
        return {"strategist": self._run_strategist, "scout": self._run_scout,
                "intelligence": self._run_intelligence, "guardian": self._run_guardian}[self.agent]()

    def _run_strategist(self):
        bb = self.bb
        self._budget("before planning")
        with bb.agent_run("strategist", "plan (individual)") as run:
            plan, meta = strategist.plan(self.question)
            run["llm_used"] = not meta.get("mock")
        bb.note_llm(meta)
        bb.update_state(plan=plan, execution_status="PLAN")
        bb.log("strategist", "create_plan", self.question,
               {"objective": plan["objective_text"], "tasks": [t["tool"] for t in plan["tasks"]]},
               reason=f"Planned by {plan['planned_by']}. Plan only: no data was collected and no other agent ran.")
        return {"type": "plan", "plan": plan}, "Completed"

    def _scout_scope(self):
        reg, c = site_registry(), self.context
        sites = [c["site"]] if c.get("site") else strategist._detect_sites(self.question, reg) or list(reg)
        detected = strategist._detect_window(self.question)
        window = {"start": c.get("start") or detected["start"], "end": c.get("end") or detected["end"]}
        tools = scout_mod.choose_tools(self.question, c.get("preferred_source"))
        def selection_reason(tool):
            if c.get("preferred_source") == tool:
                return f"The user explicitly selected {tool} as the preferred source."
            matched = [word for word in scout_mod.TOOL_KEYWORDS.get(tool, ()) if word in self.question.lower()]
            return (f"The request matched {tool} keywords: {', '.join(matched[:3])}." if matched else
                    f"{tool} is part of Scout's safe default evidence set for an ambiguous request.")
        return {"objective": self.question, "domain": "data_request", "sites": sites,
                "site_labels": [reg.get(s, {}).get("label", s) for s in sites], "window": window,
                "tasks": [{"id": f"T{i + 1}", "tool": t, "agent": "scout", "reason": selection_reason(t)}
                          for i, t in enumerate(tools)], "planned_by": "scout data request"}

    def _run_scout(self):
        bb = self.bb
        parent, parent_out = self._source()
        plan = parent_out["plan"] if parent_out else self._scout_scope()
        allowed = AGENT_POLICY["scout"]["tools"]
        for t in [t for t in plan["tasks"] if t["tool"] not in allowed]:
            bb.log("orchestrator", "policy_block", t["tool"], "blocked", reason="Tool not permitted for Scout.",
                   status="error")
        tasks = [t for t in plan["tasks"] if t["tool"] in allowed]
        faults, spec = self._faults(plan)
        if spec:
            bb.log("orchestrator", "simulation", "failure simulation enabled", spec,
                   reason="Injected faults to demonstrate recovery (demo control).", status="warn")
        bb.log("scout", "scope", {"sites": plan["site_labels"], "window": plan["window"]},
               [TOOL_NAME.get(t["tool"], t["tool"]) for t in tasks],
               reason="Data sources chosen from " + (f"the plan in {parent['code']}" if parent else "the request")
               + ".")
        scout, collected = Scout(bb, faults), {}
        for task in tasks:
            self._collect(task, scout, plan, collected)
        sources = [scout_mod.describe(c) for c in collected.values()]
        n = sum(s["records"] for s in sources)
        degraded = any(s["status"] != "ok" for s in sources)
        status = "Insufficient Data" if n == 0 else "Degraded" if degraded else "Completed"
        bb.log("scout", "deliver", f"{len(sources)} source(s)", f"{n} validated records" if n else
               scout_mod.INSUFFICIENT, reason="; ".join(f"{s['tool']}: {s['status']}" for s in sources),
               status={"Completed": "ok", "Degraded": "recovered"}.get(status, "warn"),
               fallback_used=any(s["fallback_used"] for s in sources))
        return {"type": "data", "plan": plan, "sources": sources, "collected": collected,
                "message": scout_mod.INSUFFICIENT if n == 0 else None}, status

    def _run_intelligence(self):
        bb, c = self.bb, self.context
        self._budget("before analysis")
        parent, parent_out = self._source()
        feedback = c.get("feedback")
        faults, spec = self._faults(None)
        if spec:
            bb.log("orchestrator", "simulation", "failure simulation enabled", spec,
                   reason="Injected LLM fault to demonstrate verification (demo control).", status="warn")
        with bb.agent_run("intelligence", f"revise (individual)" if feedback else "analyze (individual)") as run:
            if parent_out:
                plan, collected = parent_out["plan"], parent_out["collected"]
                result, ids = intelligence.analyze(plan, collected)
                slim = {t: {"status": v["status"], "source": v["source"],
                            "missing_fields": v.get("missing_fields") or []} for t, v in collected.items()}
                n = sum(len(v["records"]) for v in collected.values())
                basis = f"Scout data from {parent['code']}"
            else:
                result, ids, slim = intelligence.analyze_records(c.get("records"), c.get("objective") or self.question)
                n, basis = len(result["records"]), "operator-provided records"
            summary, meta = intelligence.write_summary(result, feedback=feedback, faults=faults)
            run["llm_used"] = not meta.get("mock")
        bb.note_llm(meta)
        result["summary"] = summary
        bb.update_state(result=result, facts=result["facts"], hypotheses=result["hypotheses"],
                        recommendations=result["actions"], evidence=sorted(ids), execution_status="ANALYZE")
        self._log_analysis(result, meta, feedback, n)
        bb.log("intelligence", "deliver", basis, result["title"],
               reason="Recommendations are proposals only: nothing is created until Guardian verifies and a human "
                      "approves.")
        return {"type": "analysis", "result": result, "summary": summary, "evidence_ids": sorted(ids),
                "collected_status": slim, "basis": basis, "revision_of": c.get("revision_of")}, "Completed"

    def _run_guardian(self):
        bb, c = self.bb, self.context
        self._budget("before verification")
        parent, parent_out = self._source()
        with bb.agent_run("guardian", "verify (individual)") as run:
            if parent_out:
                result = parent_out["result"]
                verdict = guardian.review(result, parent_out["summary"], set(parent_out["evidence_ids"]),
                                          parent_out["collected_status"])
                verdict |= guardian.explain(verdict, result)
                reviewed = {"kind": "intelligence_result", "execution": parent["id"], "code": parent["code"],
                            "summary": parent_out["summary"], "has_actions": bool(result["actions"])}
            else:
                report = str(c.get("report") or self.question)
                verdict = guardian.review_text(report, c.get("evidence") or "")
                reviewed = {"kind": "text", "report": report, "evidence": c.get("evidence") or ""}
            run["status"] = "ok" if verdict["verdict"] == "APPROVED" else "rejected"
        bb.update_state(guardian_verdict=verdict, execution_status="VERIFY")
        bb.log("guardian", "verify", reviewed.get("code") or "submitted conclusion", verdict["verdict"],
               reason=verdict["reason"] + " Individual mode: verdict returned to the user, no automatic revision.",
               status="ok" if verdict["verdict"] == "APPROVED" else "rejected")
        return {"type": "verdict", "verdict": verdict, "reviewed": reviewed}, "Completed"


TOOL_NAME = {"incidents": "Incident API", "telemetry": "Telemetry API", "maintenance": "Maintenance API",
             "weather": "Open-Meteo API"}


def _is_date(value):
    """Strict zero-padded YYYY-MM-DD (strptime alone accepts '2024-9-30', which breaks string comparison)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return False
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _code_of(inv_id):
    row = store.one("SELECT code FROM investigations WHERE id = ?", (inv_id,)) if inv_id is not None else None
    return row["code"] if row else str(inv_id)


def run_workflow(question, **kw):
    """FULL_WORKFLOW entry point."""
    return Orchestrator(question, **kw).run_workflow()


def run_agent(agent_id, task, context=None, failure="none", pace=0.0, parent_id=None):
    """INDIVIDUAL_AGENT entry point, e.g. run_agent("intelligence", "Find recurring patterns", {"records": ...})."""
    return Orchestrator(task, failure=failure, pace=pace, mode=INDIVIDUAL_AGENT, agent=agent_id,
                        context=context, parent_id=parent_id).run_agent()


# ---------------------------------------------------------------------- user-controlled handoffs
FINAL_OK = ("Completed", "Degraded")


def available_handoffs(inv):
    """Handoffs the USER may trigger from a finished individual execution (never automatic)."""
    if not inv or inv["mode"] != INDIVIDUAL_AGENT or inv["status"] not in FINAL_OK:
        return []
    out = store.loads(inv["result_json"], {}) or {}
    agent = inv["agent"]
    if agent == "strategist":
        return ["scout"]
    if agent == "scout":
        return ["intelligence"] if any(s["records"] for s in out.get("sources", [])) else []
    if agent == "intelligence":
        return ["guardian"]
    if agent == "guardian":
        v, rv = out.get("verdict", {}), out.get("reviewed", {})
        if v.get("verdict") == "REJECTED":
            if rv.get("kind") == "intelligence_result" or _parseable(rv.get("evidence")):
                return ["revise"]
        elif rv.get("kind") == "intelligence_result" and rv.get("has_actions") and \
                not _actions_exist_for(inv["id"], rv.get("execution"), store):
            return ["action"]
    return []


def _actions_exist_for(guardian_id, intelligence_id, db):
    """Actions already created from this Guardian run OR from any other review of the same Intelligence
    result (one recommendation must never produce two approvable action sets)."""
    if db.one("SELECT 1 AS x FROM actions WHERE investigation_id = ?", (guardian_id,)):
        return True
    return bool(intelligence_id is not None and db.one(
        "SELECT 1 AS x FROM actions a JOIN recommendations r ON r.id = a.recommendation_id "
        "WHERE r.investigation_id = ?", (intelligence_id,)))


def _parseable(text):
    try:
        intelligence.parse_records(text or "")
        return True
    except (intelligence.InputError, ValueError, TypeError):
        return False


def prepare_handoff(execution_id, target, failure="none", pace=0.0, actor="demo-user"):
    """Validate a user-requested handoff, audit it, and return the child Orchestrator (not yet run).
    target 'action' creates approval-gated actions instead and returns the list of action rows."""
    src = store.one("SELECT * FROM investigations WHERE id = ?", (execution_id,))
    if not src:
        raise KeyError("execution not found")
    allowed = available_handoffs(src)
    if target not in allowed:
        raise ValueError(f"handoff '{target}' is not available for {src['code']} (available: "
                         f"{', '.join(allowed) or 'none'})")
    if target == "action":  # handoff audit is written in the same transaction as the actions
        return create_actions(src, actor=actor)
    _audit(src["id"], "handoff", f"{src['code']} → {HANDOFF_LABELS[target]}", f"requested by {actor}",
           "User-initiated handoff. Individual Agent Mode never chains agents automatically.")
    out = store.loads(src["result_json"], {})
    if target == "revise":
        rv, v = out["reviewed"], out["verdict"]
        feedback = "; ".join(v["reasons"])
        if rv["kind"] == "intelligence_result":
            orig = store.one("SELECT * FROM investigations WHERE id = ?", (rv["execution"],))
            ctx = store.loads(orig["context_json"], {}) | {"feedback": feedback, "revision_of": orig["id"]}
            task, fail = orig["question"], "none"
        else:
            ctx = {"records": rv["evidence"], "objective": rv["report"], "feedback": feedback,
                   "revision_of": src["id"]}
            task, fail = f"Revise: {rv['report'][:200]}", "none"
        return Orchestrator(task, failure=fail, pace=pace, mode=INDIVIDUAL_AGENT, agent="intelligence",
                            context=ctx, parent_id=src["id"])
    task = {"scout": lambda: out["plan"]["objective_text"],
            "intelligence": lambda: f"Analyze data collected by {src['code']}",
            "guardian": lambda: f"Verify analysis {src['code']}"}[target]()
    return Orchestrator(task, failure=failure, pace=pace, mode=INDIVIDUAL_AGENT, agent=target,
                        context={"source_execution": src["id"]}, parent_id=src["id"])


def create_actions(guardian_exec, actor="demo-user"):
    """Guardian-approved Intelligence result -> action rows. Operational actions start as Awaiting Approval."""
    out = store.loads(guardian_exec["result_json"], {})
    if out.get("verdict", {}).get("verdict") != "APPROVED":
        raise ValueError("only Guardian-approved results can create actions")
    src = store.one("SELECT * FROM investigations WHERE id = ?", (out["reviewed"]["execution"],))
    result = store.loads(src["result_json"], {})["result"]
    rec_ids = {r["rank"]: r["id"] for r in store.query(
        "SELECT id, rank FROM recommendations WHERE investigation_id = ?", (src["id"],))}
    with store.transaction() as tx:  # actions + audit + status change land together or not at all
        if _actions_exist_for(guardian_exec["id"], src["id"], tx):
            raise ValueError(f"actions were already created for this recommendation ({src['code']})")
        _audit(guardian_exec["id"], "handoff", f"{guardian_exec['code']} → {HANDOFF_LABELS['action']}",
               f"requested by {actor}", "User-initiated handoff. Individual Agent Mode never chains agents "
               "automatically.", db=tx)
        rows = []
        for n, a in enumerate(result["actions"], 1):
            aid = _insert_action(tx, guardian_exec["id"], f"{guardian_exec['code']}-A{n}", a,
                                 "Awaiting Approval" if a["requires_approval"] else "Detected", rec_ids.get(n))
            rows.append(tx.one("SELECT * FROM actions WHERE id = ?", (aid,)))
        gated = sum(r["requires_approval"] for r in rows)
        _audit(guardian_exec["id"], "create_action", f"from {src['code']} (verified by {guardian_exec['code']})",
               f"{len(rows)} action(s); {gated} awaiting human approval",
               "Operational actions require explicit human approval before any ticket is created.", db=tx)
        tx.update("investigations", guardian_exec["id"], priority=result["priority"], site=result.get("site"),
                  status="Awaiting Approval" if gated else "Approved", updated_at=store.now())
    return rows


# ---------------------------------------------------------------------- persistence helpers
def persist_analysis(investigation_id, result, verdict=None, verified_by=None, revision=0):
    """Hypotheses, their evidence, and Intelligence recommendations. Returns {rank: recommendation id}."""
    for h in result.get("hypotheses", []):
        store.insert("hypotheses", investigation_id=investigation_id, hid=h["id"], title=h["title"],
                     confidence=h.get("confidence"), label=h.get("label"), rank=h.get("rank"),
                     payload_json=store.dumps(h))
        for rel in ("supporting", "contradicting"):
            for e in h.get(rel, []):
                store.insert("evidence", investigation_id=investigation_id, hypothesis_id=h["id"], relation=rel,
                             strength=e.get("strength"), statement=e["statement"],
                             refs_json=store.dumps(e.get("refs", [])))
        for m in h.get("missing", []):
            store.insert("evidence", investigation_id=investigation_id, hypothesis_id=h["id"], relation="missing",
                         strength="missing", statement=m["what"], refs_json="[]")
    ids = {}
    for n, a in enumerate(result.get("actions", []), 1):
        ids[n] = store.insert(
            "recommendations", investigation_id=investigation_id, rid=f"R{n}", rank=n,
            claim_type=a.get("claim_type", "RECOMMENDATION"), action_type=a.get("type"), issue=a.get("issue"),
            recommended=a.get("recommended"), priority=a.get("priority"), owner=a.get("owner"),
            effort=a.get("effort"), confidence=a.get("confidence"), hypothesis_id=a.get("hypothesis"),
            evidence_json=store.dumps(a.get("evidence", [])), reason=a.get("reason"),
            priority_reason=a.get("priority_reason"), approval_reason=a.get("approval_reason"),
            expected_impact=a.get("expected_impact"), risk=a.get("risk"),
            missing_evidence_json=store.dumps(a.get("missing_evidence", [])),
            opportunity=a.get("opportunity"), requires_approval=int(bool(a.get("requires_approval"))),
            revision=revision, guardian_verdict=verdict, verified_by_id=verified_by,
            created_at=store.now(), updated_at=store.now())
    return ids


def _insert_action(db, investigation_id, code, a, status, recommendation_id, approval_state=None):
    """db is the store module or an open store.Tx (same insert interface)."""
    approval_state = approval_state or ("pending" if a["requires_approval"] else "not_required")
    return db.insert("actions", investigation_id=investigation_id, code=code, issue=a["issue"],
                     recommended=a["recommended"], priority=a["priority"], owner=a["owner"], effort=a["effort"],
                     confidence=a.get("confidence"), evidence_json=store.dumps(a["evidence"]), reason=a["reason"],
                     priority_reason=a.get("priority_reason"), approval_reason=a.get("approval_reason"),
                     expected_impact=a.get("expected_impact"), risk=a.get("risk"),
                     missing_evidence_json=store.dumps(a.get("missing_evidence", [])),
                     opportunity=a.get("opportunity"), status=status, approval_state=approval_state,
                     requires_approval=int(a["requires_approval"]), action_type=a["type"],
                     recommendation_id=recommendation_id, created_at=store.now(), updated_at=store.now())


# ---------------------------------------------------------------------- human approval
def _audit(investigation_id, action, inp, out, reason, status="ok", db=store):
    timestamp = store.now()
    inv = db.one("SELECT mode, revisions FROM investigations WHERE id = ?", (investigation_id,)) or {}
    db.insert("audit_logs", investigation_id=investigation_id, execution_id=investigation_id,
                 ts=timestamp, timestamp=timestamp, t=None, agent="Human Approver", role="human", action=action,
                 input=inp, output=out, reason=reason, status=status, duration_ms=None, retry=0, retry_count=0,
                 revision_count=inv.get("revisions") or 0, mode=inv.get("mode") or FULL_WORKFLOW,
                 tool=None, fallback_used=0, budget_pct=None, request_json=None, response_status=None,
                 validation="not_applicable", normalized_result_json=None)


def decide_action(action_id, decision, actor="demo-user", note=""):
    """decision: 'approve' | 'reject'. Approval creates a SIMULATED ticket in the local store only.
    State check, action update, approval record, audit row and investigation status are one transaction,
    so two concurrent approvers cannot both decide the same action."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    note = (note or "").strip()
    if decision == "reject" and not note:
        raise ValueError("a rejection rationale is required")
    with store.transaction() as tx:
        a = tx.one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not a:
            raise KeyError("action not found")
        if a["status"] != "Awaiting Approval":
            raise ValueError(f"action is '{a['status']}', not awaiting approval")
        ticket = f"MT-{action_id:04d}" if decision == "approve" else None
        new_status = "Approved" if decision == "approve" else "Investigating"
        changed = tx.execute("UPDATE actions SET status = ?, approval_state = ?, ticket_ref = COALESCE(?, ticket_ref), "
                             "updated_at = ? WHERE id = ? AND status = 'Awaiting Approval'",
                             (new_status, "approved" if ticket else "rejected", ticket, store.now(), action_id))
        if changed.rowcount != 1:
            raise ValueError("action was decided concurrently")
        if ticket:
            _audit(a["investigation_id"], "approve_action", a["recommended"], f"simulated ticket {ticket} created",
                   f"Approved by {actor}. {note}".strip(), db=tx)
        else:
            _audit(a["investigation_id"], "reject_action", a["recommended"], "action rejected",
                   f"Rejected by {actor}. {note}".strip(), status="rejected", db=tx)
        tx.insert("approvals", action_id=action_id, investigation_id=a["investigation_id"], decision=decision,
                  actor=actor, note=note, previous_status=a["status"], new_status=new_status, ticket_ref=ticket,
                  ts=store.now())
        _refresh_investigation(a["investigation_id"], db=tx)
        return tx.one("SELECT * FROM actions WHERE id = ?", (action_id,))


def complete_action(action_id, actor="demo-user"):
    with store.transaction() as tx:
        a = tx.one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not a:
            raise KeyError("action not found")
        if a["status"] not in ("Approved", "Detected"):
            raise ValueError(f"action is '{a['status']}'; only Approved or Detected actions can be completed")
        tx.update("actions", action_id, status="Completed", updated_at=store.now())
        _audit(a["investigation_id"], "complete_action", a["recommended"], "completed",
               f"Marked complete by {actor}.", db=tx)
        _refresh_investigation(a["investigation_id"], db=tx)
        return tx.one("SELECT * FROM actions WHERE id = ?", (action_id,))


def _refresh_investigation(inv_id, db=store):
    inv = db.one("SELECT status FROM investigations WHERE id = ?", (inv_id,))
    if inv["status"] not in ("Awaiting Approval", "Approved"):
        return
    acts = db.query("SELECT status, requires_approval, approval_state FROM actions WHERE investigation_id = ?",
                    (inv_id,))
    gated = [a for a in acts if a["requires_approval"]]
    if any(a["status"] == "Awaiting Approval" for a in acts):
        status = "Awaiting Approval"
    elif gated and all(a["approval_state"] == "rejected" for a in gated):
        status = "Needs Review"
    elif gated and all(a["status"] == "Completed" or a["approval_state"] == "rejected" for a in gated) \
            and any(a["status"] == "Completed" for a in gated):
        status = "Completed"  # every gated action is either done or was explicitly rejected
    else:
        status = "Approved"
    db.update("investigations", inv_id, status=status, updated_at=store.now())
