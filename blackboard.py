"""
Blackboard: the shared state of one investigation run, plus the audit trail and
budget tracker the orchestrator uses as its stopping condition. Every audit
entry is written to SQLite immediately (and to output/trace.json), so even a
killed run leaves a readable partial trail.
"""
import json
import os
import threading
import time
from contextlib import contextmanager

import store
from config import AGENT_NAMES, OUTPUT_DIR


FULL_WORKFLOW = "FULL_WORKFLOW"
INDIVIDUAL_AGENT = "INDIVIDUAL_AGENT"


class Blackboard:
    def __init__(self, investigation_id, question, max_steps, max_seconds, max_llm_calls, pace=0.0,
                 mode=FULL_WORKFLOW):
        self.investigation_id = investigation_id
        self.mode = mode
        self.abandoned = None  # thread id of a timed-out worker; its later writes are dropped / marked
        self.trace = []
        self.start_time = time.time()
        # Limits below 1 would divide by zero in summary(); treat them as the smallest usable limit.
        self.max_steps, self.max_seconds, self.max_llm_calls = (max(1, max_steps), max(1, max_seconds),
                                                                max(1, max_llm_calls))
        self.step_count = self.llm_call_count = self.tool_call_count = 0
        self.current_revision = 0
        self.tokens = {"input": 0, "output": 0}
        self.pace = pace
        # This is the canonical cross-agent state for a run. The orchestrator updates it at
        # every stage; audit_events intentionally references the same list as trace.
        self.state = {
            "investigation_id": investigation_id,
            "user_request": question,
            "question": question,  # backwards-compatible alias used by trace exports
            "plan": None,
            "events": [],
            "evidence": [],
            "collected": {},
            "facts": [],
            "hypotheses": [],
            "recommendations": [],
            "result": None,
            "guardian_verdict": None,
            "execution_status": "RUNNING",
            "status": "RUNNING",  # backwards-compatible display alias
            "budget": {},
            "audit_events": self.trace,
        }

    # ---- abandoned (timed-out) worker ------------------------------------
    def abandon(self, thread_id):
        self.abandoned = thread_id

    def late(self):
        """True when called from a worker the orchestrator already gave up on."""
        return self.abandoned is not None and threading.get_ident() == self.abandoned

    @property
    def closed(self):  # backwards-compatible name
        return self.abandoned is not None

    def update_state(self, **fields):
        """Atomically update the in-memory shared state and its trace snapshot."""
        if self.late():  # never let a timed-out worker overwrite the final state
            return
        if "status" in fields and "execution_status" not in fields:
            fields["execution_status"] = fields["status"]
        if "execution_status" in fields and "status" not in fields:
            fields["status"] = fields["execution_status"]
        self.state.update(fields)
        self.state["budget"] = self.summary()
        self._persist()

    # ---- audit ---------------------------------------------------------
    def log(self, role, action, input_summary="", output_summary="", reason="", status="ok",
            duration_ms=None, retry=0, tool=None, fallback_used=False, request=None,
            response_status=None, validation=None, normalized_result=None, revision_count=None):
        if self.late():  # late output from an abandoned (timed-out) thread stays visible but is marked
            reason = f"[after timeout, result discarded] {reason}"
        entry = {
            "step": self.step_count,
            "t": round(time.time() - self.start_time, 2),
            "ts": store.now(),
            "agent": AGENT_NAMES.get(role, role),
            "role": role,
            "action": action,
            "input": _shorten(input_summary),
            "output": _shorten(output_summary),
            "reason": reason,
            "status": status,
            "duration_ms": duration_ms,
            "retry": retry,
            "mode": self.mode,
            "tool": tool,
            "fallback_used": int(bool(fallback_used)),
            # Canonical reliability/audit names; legacy aliases above remain for API compatibility.
            "execution_id": self.investigation_id,
            "timestamp": None,  # set to the same value as ts below
            "retry_count": retry,
            "revision_count": self.current_revision if revision_count is None else revision_count,
            "request_json": store.dumps(request) if request is not None else None,
            "response_status": str(response_status) if response_status is not None else None,
            "validation": validation,
            "normalized_result_json": store.dumps(normalized_result) if normalized_result is not None else None,
        }
        entry["timestamp"] = entry["ts"]
        self.trace.append(entry)
        self.step_count += 1
        entry["budget_pct"] = self.summary()["budget_used_pct"]
        self.state["budget"] = self.summary()
        store.insert("audit_logs", investigation_id=self.investigation_id, **{k: entry[k] for k in (
            "ts", "t", "agent", "role", "action", "input", "output", "reason", "status", "duration_ms", "retry",
            "mode", "tool", "fallback_used", "budget_pct", "execution_id", "timestamp", "retry_count",
            "revision_count", "request_json", "response_status", "validation", "normalized_result_json")})
        self._persist()
        if self.pace:
            time.sleep(self.pace)
        return entry

    @contextmanager
    def agent_run(self, role, task):
        """Times one agent task and records it in agent_runs (success or failure)."""
        t0 = time.time()
        run = {"status": "ok", "llm_used": 0, "error": None}
        try:
            yield run
        except Exception as e:
            run["status"], run["error"] = "error", str(e)
            raise
        finally:
            if not self.late():  # a timed-out run was already recorded by the orchestrator
                store.insert("agent_runs", investigation_id=self.investigation_id, agent=role, task=task,
                             status=run["status"], duration_ms=int((time.time() - t0) * 1000),
                             llm_used=int(bool(run["llm_used"])), error=run["error"], started_at=store.now())

    def tool_call(self, tool, source, attempt, outcome, error=None, duration_ms=0, response_status=None,
                  validation=None, fallback_used=False, records_count=None, agent="scout"):
        """One tool attempt -> tool_executions (compact per-attempt operational record)."""
        if self.late():
            return
        self.tool_call_count += 1
        store.insert("tool_executions", investigation_id=self.investigation_id, agent=agent, mode=self.mode,
                     tool=tool, source=source, attempt=attempt, outcome=outcome, error=error,
                     duration_ms=duration_ms, time=store.now(),
                     response_status=str(response_status) if response_status is not None else None,
                     validation=validation, fallback_used=int(bool(fallback_used)), records_count=records_count)

    def note_llm(self, meta):
        """Count real provider attempts, not deterministic mock generations."""
        if not meta.get("provider_call"):
            return
        self.llm_call_count += 1
        self.tokens["input"] += int(meta.get("input_tokens") or 0)
        self.tokens["output"] += int(meta.get("output_tokens") or 0)

    # ---- stopping condition ----------------------------------------------
    def budget_exceeded(self):
        if self.step_count >= self.max_steps:
            return f"step_limit ({self.max_steps} steps)"
        if time.time() - self.start_time >= self.max_seconds:
            return f"time_limit ({self.max_seconds}s)"
        if self.llm_call_count >= self.max_llm_calls:
            return f"llm_call_limit ({self.max_llm_calls} calls)"
        return None

    def summary(self):
        elapsed = time.time() - self.start_time
        used = max(self.step_count / self.max_steps, elapsed / self.max_seconds,
                   self.llm_call_count / self.max_llm_calls)
        return {
            "steps": self.step_count,
            "llm_calls": self.llm_call_count,
            "tool_calls": self.tool_call_count,
            "tokens": dict(self.tokens),
            "elapsed_seconds": round(elapsed, 2),
            "budget_used_pct": round(min(used, 1.0) * 100),
        }

    def _persist(self):
        if self.late():
            return
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        shared = {k: v for k, v in self.state.items() if k != "audit_events"}
        payload = {"investigation_id": self.investigation_id, "mode": self.mode,
                   "question": self.state["question"], "status": self.state["status"],
                   "shared_state": shared, "trace": self.trace, "budget": self.summary()}
        # Keep the legacy latest-run path for the CLI, plus a concurrency-safe per-run snapshot.
        for name in ("trace.json", f"{self.investigation_id}-trace.json"):
            with open(os.path.join(OUTPUT_DIR, name), "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)


def _shorten(value, limit=600):
    s = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "...(truncated)"
