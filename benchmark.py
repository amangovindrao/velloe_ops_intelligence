"""
Simulated benchmark: measures completion rate under random tool failures.

Methodology (also shown on the dashboard Analytics page):
  - N investigations over the demo dataset, rotating through 3 question types
  - every primary data-source attempt fails with probability FAULT_RATE
    (random 500 / timeout / malformed); fallbacks are left healthy
  - "completed" = the run ends in a Guardian verdict (Approved / Awaiting
    Approval / Needs Review) instead of crashing or stopping on budget
Each invocation is one benchmark_runs row (parameters, per-run results, completion
rate); its investigations are stored with is_benchmark=1 and benchmark_run_id so they
stay separate from real investigations.
"""
import argparse
import json
import os
import time

os.environ.setdefault("SCOUT_TIMEOUT_SECONDS", "0.5")   # keep simulated timeouts short
os.environ.setdefault("SCOUT_BACKOFF_SECONDS", "0.05")

import store  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402  (env must be set first)

METHODOLOGY = ("Simulated benchmark (python benchmark.py): N investigations over the demo dataset, 3 rotating "
               "question types, each primary data-source attempt fails with the configured probability (random "
               "500 / timeout / malformed). Completed = run ends with a Guardian verdict instead of crashing or "
               "stopping on budget.")

JUDGE_QUESTION = "Why are repeated power fluctuations happening at Site A, and what should we do?"
QUESTIONS = [
    "Investigate the temperature anomaly at Site A.",
    "Why did Site A have repeated network outages?",
    JUDGE_QUESTION,
]
COMPLETED = ("Approved", "Awaiting Approval", "Needs Review", "Completed")


def run_benchmark(n=6, fault_rate=0.5, seed=7, quiet=False):
    store.init()
    run_id = store.insert("benchmark_runs", status="running", n_runs=n, fault_rate=fault_rate, seed=seed,
                          methodology=METHODOLOGY, started_at=store.now())
    results = []
    try:
        for i in range(n):
            q = QUESTIONS[i % len(QUESTIONS)]
            t0 = time.time()
            res = Orchestrator(q, fault_spec="", fault_rate=fault_rate, seed=seed + i, is_benchmark=True,
                               benchmark_run_id=run_id).run()
            results.append({"run": i + 1, "investigation_id": res["id"], "code": res["code"], "question": q,
                            "status": res["status"], "completed": res["status"] in COMPLETED,
                            "confidence": res["confidence"], "revisions": res["revisions"],
                            "seconds": round(time.time() - t0, 2)})
    except Exception as e:  # keep the partial benchmark visible instead of a dangling "running" row
        store.update("benchmark_runs", run_id, status="error", error=str(e), results_json=store.dumps(results),
                     completed_runs=sum(r["completed"] for r in results), finished_at=store.now())
        raise
    done = sum(r["completed"] for r in results)
    store.update("benchmark_runs", run_id, status="completed", completed_runs=done,
                 completion_rate=round(done / n * 100, 1) if n else None,
                 avg_seconds=round(sum(r["seconds"] for r in results) / n, 2) if n else None,
                 results_json=store.dumps(results), finished_at=store.now())
    if not quiet:
        print(json.dumps(results, indent=2))
        print(f"\nSimulated benchmark #{run_id}: completion {done}/{n} = {done / max(n, 1) * 100:.0f}% "
              f"at {int(fault_rate * 100)}% injected primary-source failure rate (stored in benchmark_runs)")
    return results


DETERMINISTIC_METHODOLOGY = ("Deterministic acceptance benchmark: fixed Normal, API failure, Timeout, Malformed "
                             "response, No data, Guardian rejection/revision, and Safe stopping scenarios over "
                             "simulated Ops data. No result is altered; each scenario records expected-vs-actual.")
DETERMINISTIC_QUESTION = "Why did Site A have repeated network outages?"
DETERMINISTIC_SCENARIOS = (
    {"name": "normal", "fault_spec": "", "expect": "completion"},
    {"name": "api_failure", "fault_spec": "incidents=500", "expect": "recovery"},
    {"name": "timeout", "fault_spec": "incidents=timeout+timeout+timeout", "expect": "fallback"},
    {"name": "malformed_response", "fault_spec": "incidents=malformed+malformed+malformed", "expect": "fallback"},
    {"name": "no_data", "fault_spec": "incidents=no_data+no_data+no_data", "expect": "fallback"},
    {"name": "guardian_rejection", "fault_spec": "intelligence=hallucinate", "expect": "revision"},
    {"name": "judge_power", "question": JUDGE_QUESTION,
     "fault_spec": "incidents=500+timeout+malformed,weather=down", "expect": "revision"},
    {"name": "safe_stopping", "fault_spec": "", "expect": "stopped", "max_steps": 1},
)


def run_deterministic_benchmark(quiet=False):
    """Run the fixed judge matrix and persist unmodified per-scenario measurements."""
    store.init()
    run_id = store.insert("benchmark_runs", status="running", n_runs=len(DETERMINISTIC_SCENARIOS),
                          fault_rate=0.0, seed=0, methodology=DETERMINISTIC_METHODOLOGY,
                          started_at=store.now())
    results = []
    for scenario in DETERMINISTIC_SCENARIOS:
        t0 = time.time()
        question = scenario.get("question", DETERMINISTIC_QUESTION)
        orch = Orchestrator(question, fault_spec=scenario["fault_spec"], is_benchmark=True,
                            benchmark_run_id=run_id)
        if scenario.get("max_steps") is not None:
            orch.bb.max_steps = scenario["max_steps"]
        summary = orch.run()
        tools = store.query("SELECT * FROM tool_executions WHERE investigation_id = ? ORDER BY id", (summary["id"],))
        audit = store.query("SELECT * FROM audit_logs WHERE investigation_id = ? ORDER BY id", (summary["id"],))
        retries = sum(t["outcome"] not in ("ok", "fallback_ok") for t in tools)
        fallback = any(t["outcome"] == "fallback_ok" for t in tools)
        recovery = fallback or any(t["attempt"] > 1 and t["outcome"] == "ok" for t in tools)
        rejections = sum(a["role"] == "guardian" and a["status"] == "rejected" for a in audit)
        safe_stopping = summary["status"] == "Stopped" and any(
            a["action"] == "stop" and "STOPPED SAFELY" in (a["output"] or "") for a in audit)
        completed = summary["status"] in COMPLETED
        expectation = scenario["expect"]
        passed = {"completion": completed, "recovery": completed and recovery,
                  "fallback": completed and fallback, "revision": completed and rejections > 0
                  and summary["revisions"] > 0, "stopped": safe_stopping}[expectation]
        results.append({"scenario": scenario["name"], "expectation": expectation, "passed": bool(passed),
                        "status": summary["status"], "completed": completed, "recovered": recovery,
                        "fallback_used": fallback, "retries": retries, "rejections": rejections,
                        "revisions": summary["revisions"], "safe_stopping": safe_stopping,
                        "seconds": round(time.time() - t0, 3), "investigation_id": summary["id"],
                        "code": summary["code"], "question": question})
    passed_count = sum(r["passed"] for r in results)
    store.update("benchmark_runs", run_id, status="completed" if passed_count == len(results) else "failed",
                 completed_runs=passed_count, completion_rate=round(passed_count / len(results) * 100, 1),
                 avg_seconds=round(sum(r["seconds"] for r in results) / len(results), 3),
                 results_json=store.dumps(results), finished_at=store.now())
    if not quiet:
        print(json.dumps(results, indent=2))
        print(f"\nDeterministic benchmark #{run_id}: {passed_count}/{len(results)} scenarios passed")
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=("deterministic", "random"), default="deterministic")
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--rate", type=float, default=0.5)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    if args.suite == "deterministic":
        run_deterministic_benchmark()
    else:
        run_benchmark(args.n, args.rate, args.seed)
