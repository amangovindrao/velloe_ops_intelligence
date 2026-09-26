"""
Core tests for Velloe Ops Intelligence (stdlib unittest, no network).

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="velloe_test_")
os.environ.update(MOCK_MODE="on", DB_PATH=os.path.join(TMP, "test.db"), OUTPUT_DIR=TMP,
                  SCOUT_TIMEOUT_SECONDS="0.3", SCOUT_BACKOFF_SECONDS="0", SIM_API_LATENCY_SECONDS="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
from agents import guardian, intelligence, strategist  # noqa: E402
from agents.scout import EXPECTED_FIELDS, Scout  # noqa: E402
from config import AGENTS  # noqa: E402
from tools import ops_api, weather_tool  # noqa: E402
from tools.faults import SCENARIOS, FaultInjector, build, ui_failure_spec  # noqa: E402

JUDGE_Q = "Why are repeated power fluctuations happening at Site A, and what should we do?"
THERMAL_Q = "Investigate the temperature anomaly at Site A."
NO_NET = "weather=down"  # tests never call the real weather API; the cached export is used


class FakeBB:
    def __init__(self):
        self.logs, self.calls = [], []

    def log(self, *a, **k):
        self.logs.append((a, k))

    def tool_call(self, *a, **k):
        self.calls.append((a, k))


def plan_for(q=THERMAL_Q):
    p, _ = strategist.plan(q)
    return p


def collect(plan, spec=NO_NET):
    scout = Scout(FakeBB(), build(spec))
    return {t["tool"]: scout.collect(t, plan) for t in plan["tasks"]}


class TestFaultsAndNormalization(unittest.TestCase):
    def test_fault_queue_and_sticky(self):
        f = FaultInjector("telemetry=500+timeout,weather=down")
        self.assertEqual([f.next("telemetry") for _ in range(3)], ["500", "timeout", None])
        self.assertEqual([f.next("weather") for _ in range(2)], ["down", "down"])

    def test_unknown_fault_rejected(self):
        with self.assertRaises(ValueError):
            FaultInjector("telemetry=explode")

    def test_primary_and_fallback_normalize_identically(self):
        for ep in ops_api.ENDPOINTS:
            a = ops_api.normalize(ep, ops_api.ops_api_fetch(ep, FaultInjector(), 1))
            b = ops_api.normalize(ep, ops_api.snapshot_fetch(ep, FaultInjector(), 1))
            key = lambda r: r["id"]  # noqa: E731
            self.assertEqual(sorted(a, key=key), sorted(b, key=key), ep)

    def test_malformed_payload_detected(self):
        raw = ops_api.ops_api_fetch("incidents", FaultInjector("incidents=malformed"), 1)
        with self.assertRaises(ValueError):
            ops_api.normalize("incidents", raw)


class TestStrategist(unittest.TestCase):
    def test_dynamic_plans(self):
        thermal, network, power = plan_for(), plan_for("Why did Site A have repeated network outages?"), \
            plan_for("Investigate power events at Site B.")
        self.assertEqual((thermal["domain"], thermal["sites"]), ("thermal", ["NOI-DC1"]))
        self.assertIn("weather", [t["tool"] for t in thermal["tasks"]])
        self.assertEqual(network["domain"], "network")
        self.assertIn("maintenance", [t["tool"] for t in network["tasks"]])
        self.assertEqual((power["domain"], power["sites"]), ("power", ["GGN-EDGE2"]))
        self.assertIn("weather", [t["tool"] for t in power["tasks"]])
        self.assertNotIn("maintenance", [t["tool"] for t in power["tasks"]])


class TestScout(unittest.TestCase):
    task = {"tool": "telemetry"}

    def run_scout(self, spec):
        bb = FakeBB()
        return Scout(bb, build(spec)).collect(self.task, plan_for()), bb

    def test_retry_then_success(self):
        res, bb = self.run_scout("telemetry=500")
        self.assertEqual((res["status"], res["attempts"]), ("ok", 2))

    def test_fallback_after_retry_limit(self):
        res, _ = self.run_scout("telemetry=500+timeout+malformed")
        self.assertEqual(res["status"], "degraded_fallback")
        self.assertEqual(len(res["errors"]), 3)
        self.assertIn("timed out", res["errors"][1])
        self.assertIn("malformed", res["errors"][2])
        self.assertTrue(res["records"])

    def test_timeout_is_enforced(self):
        t0 = time.time()
        with mock.patch("agents.scout.SCOUT_TIMEOUT_SECONDS", 0.3):
            self.run_scout("telemetry=timeout+timeout+timeout")
        self.assertLess(time.time() - t0, 2.5)  # 3 x 0.3 s deadlines, not 3 x the tool's sleep

    def test_graceful_degradation_never_fabricates(self):
        res, _ = self.run_scout("telemetry=down,snapshot:telemetry=down")
        self.assertEqual((res["status"], res["records"]), ("failed", []))
        self.assertEqual(res["missing_fields"][0]["fields"], list(EXPECTED_FIELDS["telemetry"]))


class TestIntelligenceAndGuardian(unittest.TestCase):
    def analyse(self, extra_tools=("maintenance",), spec=NO_NET):
        p = plan_for()
        strategist.add_tasks(p, list(extra_tools))
        collected = collect(p, spec)
        result, ids = intelligence.analyze(p, collected)
        return result, ids, collected

    def test_cooling_hypothesis_leads_with_evidence(self):
        r, ids, _ = self.analyse()
        top = r["hypotheses"][0]
        self.assertEqual(top["kind"], "cooling")
        self.assertGreaterEqual(top["confidence"], 70)
        refs = {x for e in top["supporting"] for x in e["refs"]}
        self.assertIn("MNT-302", refs)
        self.assertTrue(refs <= ids)
        self.assertEqual(len({h["id"] for h in r["hypotheses"]}), len(r["hypotheses"]))
        self.assertTrue(r["facts"])
        self.assertTrue(all(fact["claim_type"] == "FACT" and "confidence" not in fact for fact in r["facts"]))
        self.assertTrue(r["evidence"] and all(e["claim_type"] == "EVIDENCE" for e in r["evidence"]))
        self.assertTrue(all(h["claim_type"] == "HYPOTHESIS" for h in r["hypotheses"]))
        self.assertIs(r["recommendations"], r["actions"])
        self.assertTrue(all(a["claim_type"] == "RECOMMENDATION" for a in r["recommendations"]))

    def test_weather_hypothesis_is_contradicted(self):
        r, _, _ = self.analyse()
        wx = next(h for h in r["hypotheses"] if h["kind"] == "weather")
        self.assertTrue(wx["contradicting"])
        self.assertLess(wx["confidence"], 40)

    def test_actions_have_required_fields(self):
        r, _, _ = self.analyse()
        for a in r["actions"]:
            for k in ("issue", "priority", "owner", "effort", "recommended", "reason"):
                self.assertTrue(a[k], k)
            self.assertIn(a["priority"], ("Critical", "High", "Medium", "Low"))
        self.assertTrue(r["actions"][0]["requires_approval"])
        self.assertTrue(r["opportunities"])

    def test_guardian_approves_clean_result(self):
        r, ids, col = self.analyse()
        summary, _ = intelligence.write_summary(r)
        self.assertEqual(guardian.review(r, summary, ids, col)["verdict"], "APPROVED")

    def test_guardian_rejects_hallucination_and_overclaiming(self):
        r, ids, col = self.analyse()
        v = guardian.review(r, "This is definitely the confirmed root cause [INC-9999].", ids, col)
        self.assertEqual(v["verdict"], "REJECTED")
        failed = {c["check"] for c in v["checks"] if not c["passed"]}
        self.assertTrue({"evidence_exists", "no_overclaiming"} <= failed)

    def test_guardian_requests_missing_evidence(self):
        r, ids, col = self.analyse(extra_tools=())
        summary, _ = intelligence.write_summary(r)
        v = guardian.review(r, summary, ids, col)
        self.assertEqual((v["verdict"], v["evidence_requests"]), ("REJECTED", ["maintenance"]))

    def test_guardian_requires_disclosure_of_degraded_sources(self):
        r, ids, col = self.analyse(spec="telemetry=down," + NO_NET)
        r["unknowns"] = []
        summary, _ = intelligence.write_summary(r)
        failed = {c["check"] for c in guardian.review(r, summary, ids, col)["checks"] if not c["passed"]}
        self.assertIn("gaps_disclosed", failed)


class TestOrchestrator(unittest.TestCase):
    def test_end_to_end_recovery_revision_and_approval(self):
        res = orchestrator.Orchestrator(THERMAL_Q, fault_spec="telemetry=500+500+500," + NO_NET).run()
        self.assertEqual(res["status"], "Awaiting Approval")
        self.assertEqual(res["revisions"], 1)  # Guardian asked for maintenance data once
        logs = store.query("SELECT action FROM audit_logs WHERE investigation_id = ?", (res["id"],))
        actions = [l["action"] for l in logs]
        for step in ("create_plan", "tool_failure", "fallback", "verify", "delegate_more_evidence", "complete"):
            self.assertIn(step, actions)
        a = store.one("SELECT * FROM actions WHERE investigation_id = ? AND status = 'Awaiting Approval'", (res["id"],))
        done = orchestrator.decide_action(a["id"], "approve")
        self.assertEqual((done["status"], done["ticket_ref"]), ("Approved", f"MT-{a['id']:04d}"))
        with self.assertRaises(ValueError):
            orchestrator.decide_action(a["id"], "approve")  # no double approval
        inv = store.one("SELECT status FROM investigations WHERE id = ?", (res["id"],))
        self.assertEqual(inv["status"], "Approved")
        for table in ("events", "evidence", "hypotheses", "agent_runs", "tool_calls", "approvals"):
            self.assertTrue(store.query(f"SELECT 1 FROM {table} LIMIT 1"), table)

    def test_shared_blackboard_and_exact_agent_delegation(self):
        runner = orchestrator.Orchestrator(THERMAL_Q, fault_spec=NO_NET)
        res = runner.run()
        state = runner.bb.state
        self.assertEqual(AGENTS, ("strategist", "scout", "intelligence", "guardian"))
        self.assertEqual((state["investigation_id"], state["user_request"]), (res["id"], THERMAL_Q))
        for field in ("plan", "collected", "facts", "hypotheses", "recommendations",
                      "result", "guardian_verdict", "budget", "audit_events"):
            self.assertTrue(state[field], field)
        self.assertEqual(state["execution_status"], res["status"])
        delegated = {row["agent"] for row in store.query(
            "SELECT agent FROM agent_runs WHERE investigation_id = ?", (res["id"],))}
        self.assertEqual(delegated, set(AGENTS))

    def test_revision_loop_is_bounded(self):
        spec = "intelligence=hallucinate+hallucinate+hallucinate+hallucinate+hallucinate," + NO_NET
        with mock.patch.object(orchestrator, "MAX_REVISIONS", 2):
            res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=spec).run()
        self.assertEqual((res["status"], res["revisions"]), ("Needs Review", 2))
        pending = store.query("SELECT 1 FROM actions WHERE investigation_id = ? AND status = 'Awaiting Approval'",
                              (res["id"],))
        self.assertFalse(pending)  # unverified results never reach the approval gate

    def test_budget_stops_safely(self):
        with mock.patch.object(orchestrator, "MAX_STEPS", 4):
            res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=NO_NET).run()
        self.assertEqual(res["status"], "Stopped")

    def test_total_blackout_still_completes(self):
        spec = "incidents=down,telemetry=down,maintenance=down,weather=down,snapshot:telemetry=down,weather_cache=down"
        res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=spec).run()
        self.assertIn(res["status"], ("Approved", "Awaiting Approval", "Needs Review"))


POWER_RECORDS = ("10:05 Power fluctuation\n10:08 UPS warning\n10:12 Server warning\n\n"
                 "10:45 Power fluctuation\n10:48 UPS warning\n10:51 Server warning")


def row(i):
    return store.one("SELECT * FROM investigations WHERE id = ?", (i,))


def output(i):
    return store.loads(row(i)["result_json"], {})


def audit_actions(i):
    return [a["action"] for a in store.query("SELECT action FROM audit_logs WHERE investigation_id = ? ORDER BY id",
                                             (i,))]


def individual(agent, task, context=None, **kw):
    return orchestrator.Orchestrator(task, mode=orchestrator.INDIVIDUAL_AGENT, agent=agent, context=context,
                                     **kw).run_agent()


class TestIndividualAgentMode(unittest.TestCase):
    def roles(self, i):
        return {r["agent"] for r in store.query("SELECT agent FROM agent_runs WHERE investigation_id = ?", (i,))}

    # 1
    def test_strategist_plans_without_running_other_agents(self):
        res = orchestrator.run_agent("strategist", "Create an investigation plan for repeated power fluctuations "
                                                   "at Site A.")
        self.assertEqual((res["status"], res["mode"]), ("Completed", "INDIVIDUAL_AGENT"))
        plan = output(res["id"])["plan"]
        for key in ("objective_text", "steps", "required_data", "potential_tools", "risks"):
            self.assertTrue(plan[key], key)
        self.assertEqual(plan["domain"], "power")
        self.assertEqual(self.roles(res["id"]), {"strategist"})
        self.assertFalse(store.query("SELECT 1 FROM tool_calls WHERE investigation_id = ?", (res["id"],)))

    # 2
    def test_scout_fetches_normalizes_and_reports_metadata(self):
        res = individual("scout", "Fetch the latest incident data for Site A.", {"site": "NOI-DC1"})
        self.assertEqual(res["status"], "Completed")
        src = output(res["id"])["sources"][0]
        self.assertEqual((src["tool"], src["status"], src["retries"], src["fallback_used"]), ("incidents", "ok", 0, False))
        self.assertIn("resolved_at", src["missing_fields"])  # unavailable fields are reported, not invented
        recs = output(res["id"])["collected"]["incidents"]["records"]
        self.assertTrue(recs and all(r["site"] == "NOI-DC1" for r in recs))
        self.assertEqual(self.roles(res["id"]), {"scout"})

    # 3
    def test_intelligence_analyzes_records_without_claiming_causation(self):
        res = orchestrator.run_agent("intelligence", "Find recurring patterns", {"records": POWER_RECORDS})
        self.assertEqual(res["status"], "Completed")
        out = output(res["id"])
        r = out["result"]
        self.assertIn("Recurring sequence", r["patterns"][0]["statement"])
        top = r["hypotheses"][0]
        self.assertEqual(top["kind"], "power")
        self.assertTrue(top["supporting"] and top["missing"])
        self.assertLess(top["confidence"], 60)  # timing alone must not produce a confident root cause
        self.assertIn("heuristic", r["confidence_method"])
        self.assertIn("correlation does not establish causation", out["summary"])
        self.assertNotIn("definitely", out["summary"].lower())
        for a in r["actions"]:
            self.assertTrue(all(a[k] for k in ("priority", "owner", "effort", "recommended")))
        self.assertEqual(self.roles(res["id"]), {"intelligence"})

    # 4 + 9
    def test_guardian_rejects_unsupported_causal_claim(self):
        res = orchestrator.run_agent("guardian", "Review this conclusion", {
            "report": "The UPS is definitely defective because UPS warnings happened after two power fluctuations."})
        v = output(res["id"])["verdict"]
        self.assertEqual(v["verdict"], "REJECTED")
        failed = {c["check"] for c in v["checks"] if not c["passed"]}
        self.assertTrue({"no_overclaiming", "correlation_vs_causation", "sufficient_sample"} <= failed)
        self.assertIn("UPS diagnostic logs", v["missing_evidence"])
        self.assertTrue(v["required_revision"] and v["unsupported_claims"] and v["risk"])
        self.assertTrue(v["reason"] and v["issues"] and v["missing_evidence"])
        self.assertEqual(self.roles(res["id"]), {"guardian"})
        self.assertNotIn("revision", audit_actions(res["id"]))  # no automatic revision loop

    def test_guardian_approves_hedged_conclusion(self):
        res = orchestrator.run_agent("guardian", "Review", {
            "report": "Repeated UPS warnings following power fluctuations suggest a UPS-related or upstream "
                      "power-quality issue may be contributing. UPS diagnostic logs are required to confirm it.",
            "evidence": "10:05 Power fluctuation\n10:08 UPS warning"})
        self.assertEqual(output(res["id"])["verdict"]["verdict"], "APPROVED")

    # 5
    def test_scout_api_failure_then_retry_success(self):
        res = individual("scout", "Fetch incidents for Site A", {"preferred_source": "incidents"},
                         fault_spec="incidents=500")
        src = output(res["id"])["sources"][0]
        self.assertEqual((res["status"], src["status"], src["retries"], src["fallback_used"]),
                         ("Completed", "ok", 1, False))
        self.assertIn("tool_failure", audit_actions(res["id"]))

    # 6
    def test_scout_fallback_and_insufficient_data(self):
        res = orchestrator.run_agent("scout", "Fetch incidents for Site A", {"preferred_source": "incidents"},
                                     failure="api_500")
        src = output(res["id"])["sources"][0]
        self.assertEqual((res["status"], src["status"], src["fallback_used"]), ("Degraded", "degraded_fallback", True))
        self.assertTrue(src["records"])
        logs = store.query("SELECT * FROM audit_logs WHERE investigation_id = ? AND action = 'fallback'", (res["id"],))
        self.assertEqual(logs[0]["fallback_used"], 1)
        down = orchestrator.run_agent("scout", "Fetch incidents", {"preferred_source": "incidents"}, failure="outage")
        self.assertEqual(down["status"], "Insufficient Data")
        self.assertEqual(output(down["id"])["message"], "Insufficient evidence. No reliable operational data is available.")
        self.assertEqual(output(down["id"])["collected"]["incidents"]["records"], [])

    # 7
    def test_individual_execution_timeout(self):
        with mock.patch.object(orchestrator, "AGENT_TIMEOUT_SECONDS", 0.4):
            res = orchestrator.run_agent("scout", "Fetch incidents", {"preferred_source": "incidents"},
                                         failure="timeout")
        self.assertEqual(res["status"], "Timeout")
        self.assertIn("timeout", audit_actions(res["id"]))
        self.assertIsNone(row(res["id"])["result_json"])  # partial results are discarded
        time.sleep(1.5)  # let the abandoned worker finish before other tests

    def test_validation_and_tool_policy(self):
        bad = [("scout", "Fetch", {"preferred_source": "email"}), ("scout", "Fetch", {"site": "MARS"}),
               ("intelligence", "x", {"records": "no timestamps"}), ("guardian", "", {}),
               ("strategist", "plan", {})]
        for agent, task, ctx in bad[:4]:
            res = orchestrator.run_agent(agent, task, ctx)
            self.assertEqual(res["status"], "Validation Failed", (agent, ctx))
            self.assertIn("validation_failed", audit_actions(res["id"]))
        res = orchestrator.run_agent("strategist", "plan", {}, failure="api_500")  # not a Strategist fault
        self.assertEqual(res["status"], "Validation Failed")

    # 8
    def test_individual_execution_audit_logging(self):
        res = orchestrator.run_agent("scout", "Fetch incidents", {"preferred_source": "incidents"}, failure="api_500")
        logs = store.query("SELECT * FROM audit_logs WHERE investigation_id = ? ORDER BY id", (res["id"],))
        self.assertTrue(all(l["mode"] == "INDIVIDUAL_AGENT" for l in logs))
        self.assertEqual((logs[0]["action"], logs[-1]["action"]), ("execution_start", "execution_complete"))
        calls = [l for l in logs if l["role"] == "scout" and l["tool"]]
        self.assertTrue(calls and all(l["budget_pct"] is not None for l in logs))
        self.assertEqual(max(l["retry"] for l in calls), 2)

    # 10 + 11: demo chain Intelligence -> Guardian (reject) -> revise -> Guardian (approve) -> action -> human
    def test_optional_handoffs_and_approval_gate(self):
        intel = orchestrator.run_agent("intelligence", "Find recurring patterns", {"records": POWER_RECORDS},
                                       failure="overclaim")
        self.assertEqual(orchestrator.available_handoffs(row(intel["id"])), ["guardian"])
        self.assertEqual(self.roles(intel["id"]), {"intelligence"})  # no silent chaining
        self.assertFalse(store.query("SELECT 1 FROM actions WHERE investigation_id = ?", (intel["id"],)))
        with self.assertRaises(ValueError):
            orchestrator.prepare_handoff(intel["id"], "action")  # unverified results cannot create actions

        g1 = orchestrator.prepare_handoff(intel["id"], "guardian").run_agent()
        self.assertEqual(output(g1["id"])["verdict"]["verdict"], "REJECTED")
        self.assertEqual(orchestrator.available_handoffs(row(g1["id"])), ["revise"])
        rev = orchestrator.prepare_handoff(g1["id"], "revise").run_agent()
        self.assertEqual(output(rev["id"])["revision_of"], intel["id"])
        g2 = orchestrator.prepare_handoff(rev["id"], "guardian").run_agent()
        self.assertEqual(output(g2["id"])["verdict"]["verdict"], "APPROVED")
        self.assertEqual(orchestrator.available_handoffs(row(g2["id"])), ["action"])
        for ex in (intel, g1, rev):
            self.assertIn("handoff", audit_actions(ex["id"]))

        acts = orchestrator.prepare_handoff(g2["id"], "action")
        gated = [a for a in acts if a["requires_approval"]]
        self.assertTrue(gated)
        self.assertEqual(row(g2["id"])["status"], "Awaiting Approval")
        self.assertTrue(all(a["status"] == "Awaiting Approval" and not a["ticket_ref"] for a in gated))
        with self.assertRaises(ValueError):
            orchestrator.complete_action(gated[0]["id"])  # cannot skip the human approval gate
        with self.assertRaises(ValueError):
            orchestrator.prepare_handoff(g2["id"], "action")  # actions are created once
        done = orchestrator.decide_action(gated[0]["id"], "approve", actor="tester")
        self.assertEqual((done["status"], done["ticket_ref"]), ("Approved", f"MT-{gated[0]['id']:04d}"))
        self.assertIn("approve_action", audit_actions(g2["id"]))

    def test_plan_to_scout_to_intelligence_handoff(self):
        plan = orchestrator.run_agent("strategist", "Create an investigation plan for repeated power fluctuations "
                                                    "at Site B.")
        scout = orchestrator.prepare_handoff(plan["id"], "scout").run_agent()
        self.assertIn(scout["status"], ("Completed", "Degraded"))
        tools = {s["tool"] for s in output(scout["id"])["sources"]}
        self.assertEqual(tools, {t["tool"] for t in output(plan["id"])["plan"]["tasks"]})
        intel = orchestrator.prepare_handoff(scout["id"], "intelligence").run_agent()
        self.assertEqual(intel["status"], "Completed")
        self.assertEqual(row(intel["id"])["parent_id"], scout["id"])

    # 12
    def test_full_workflow_still_primary_and_unchanged(self):
        res = orchestrator.run_workflow(THERMAL_Q, fault_spec="telemetry=500+500+500," + NO_NET)
        self.assertEqual((res["status"], res["mode"], res["revisions"]), ("Awaiting Approval", "FULL_WORKFLOW", 1))
        self.assertTrue(res["code"].startswith("VX-"))
        self.assertEqual(self.roles(res["id"]), set(AGENTS))
        modes = {l["mode"] for l in store.query("SELECT mode FROM audit_logs WHERE investigation_id = ?", (res["id"],))}
        self.assertEqual(modes, {"FULL_WORKFLOW"})


class TestOperationsConsole(unittest.TestCase):
    def test_action_board_projection_and_hybrid_ui_contract(self):
        inv_id = store.insert("investigations", code="VX-UI001", question="UI action", title="UI action test",
                              site="Site A", status="Awaiting Approval", mode="FULL_WORKFLOW", is_benchmark=0,
                              created_at=store.now(), updated_at=store.now())
        action_id = store.insert("actions", investigation_id=inv_id, code="VX-UI001-A1", issue="Cooling risk",
                                 recommended="Inspect CRAC-2", priority="High", owner="Facilities", effort="Low",
                                 confidence=80, evidence_json=store.dumps(["MNT-302"]), reason="Repeated warning",
                                 opportunity="Automate redundancy checks", status="Awaiting Approval",
                                 approval_state="pending", requires_approval=1, action_type="maintenance_ticket",
                                 created_at=store.now(), updated_at=store.now())
        projected = next(a for a in server.actions() if a["id"] == action_id)
        for field in ("issue", "site", "priority", "owner", "recommended", "approval_state", "status",
                      "opportunity", "evidence", "investigation_code", "investigation_mode"):
            self.assertIn(field, projected)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as f:
            app = f.read()
        for token in ("data-board-view", "Kanban", "Table", "actionTable", "Automation opportunity",
                      "Issue / Site", "Approval", "Status"):
            self.assertIn(token, app)

    def test_composable_audit_api_and_expandable_ui_contract(self):
        result = orchestrator.run_agent("scout", "Fetch incidents", {"preferred_source": "incidents"},
                                        failure="api_500")
        failure_rows = server.audit({"agent": ["scout"], "execution": [str(result["id"])],
                                     "status": ["warn"], "event": ["failure"]})
        recovery_rows = server.audit({"agent": ["scout"], "execution": [str(result["id"])],
                                      "event": ["recovery"]})
        self.assertTrue(failure_rows and all(r["role"] == "scout" and r["status"] == "warn"
                                             for r in failure_rows))
        self.assertTrue(recovery_rows and all(r["fallback_used"] or r["status"] == "recovered"
                                              for r in recovery_rows))
        day = (failure_rows[0]["timestamp"] or failure_rows[0]["ts"])[:10]
        dated = server.audit({"execution": [str(result["id"])], "from": [day], "to": [day],
                              "search": [result["code"]]})
        self.assertTrue(dated)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as f:
            app = f.read()
        for token in ("af-search", "af-agent", "af-investigation", "af-execution", "af-status", "af-event",
                      "af-from", "af-to", "request_json", "response_status", "normalized_result_json"):
            self.assertIn(token, app)

    def test_shell_includes_live_individual_execution(self):
        result = orchestrator.run_agent("strategist", "Plan a power investigation")
        thread = mock.Mock()
        thread.is_alive.return_value = True
        server.RUNNING[result["id"]] = thread
        try:
            overview = server.overview()
            self.assertIn(result["id"], {r["id"] for r in overview["running_executions"]})
            self.assertEqual(overview["health"]["components"]["Orchestrator"], "Running")
            self.assertEqual(overview["health"]["components"]["Strategist"], "Running")
            self.assertEqual(set(overview["health"]["components"]),
                             {"Orchestrator", "Strategist", "Scout", "Intelligence", "Guardian", "Tool Layer", "Audit"})
        finally:
            server.RUNNING.pop(result["id"], None)

    def test_light_dark_and_responsive_console_contract(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn('<html lang="en" data-theme="light">', page)
        for token in ('[data-theme="dark"]', '@media (max-width: 1180px)', '@media (max-width: 900px)',
                      '.filter-grid', '.audit-detail-grid', 'VELLOE OPS', 'System Status'):
            self.assertIn(token, page)
        meta = server.sites()
        self.assertEqual(set(meta["failures"]), set(AGENTS))
        self.assertIn("normal", meta["failures"]["scout"])
        self.assertNotIn("none", meta["failures"]["scout"])


class TestReliabilityJudgeContract(unittest.TestCase):
    def test_real_open_meteo_response_is_validated_normalized_and_audited(self):
        plan = plan_for()
        body = {"daily": {"time": ["2024-06-11", "2024-06-12"],
                          "temperature_2m_max": [42.1, 43.2], "temperature_2m_min": [30.0, 31.0]}}
        response = mock.Mock(status_code=200, text=json.dumps(body))
        response.raise_for_status.return_value = None
        bb = FakeBB()
        with mock.patch.object(weather_tool.requests, "get", return_value=response) as get:
            result = Scout(bb, build()).collect({"tool": "weather"}, plan)
        self.assertEqual((result["status"], result["response_status"], result["validation"]),
                         ("ok", "200", "validated"))
        self.assertEqual(result["normalized_result"]["count"], 2)
        self.assertEqual(result["records"][0]["id"], "WX-NOI-DC1-2024-06-11")
        self.assertEqual(result["request"]["url"], weather_tool.ARCHIVE_URL)
        self.assertEqual(get.call_args.kwargs["params"]["timezone"], "Asia/Kolkata")
        audit_kwargs = next(kwargs for args, kwargs in bb.logs if args[1] == "tool_call")
        self.assertEqual((audit_kwargs["response_status"], audit_kwargs["validation"]), (200, "validated"))
        self.assertEqual(audit_kwargs["normalized_result"]["count"], 2)

    def test_http_429_retries_then_succeeds(self):
        result = Scout(FakeBB(), build("telemetry=429")).collect({"tool": "telemetry"}, plan_for())
        self.assertEqual((result["status"], result["attempts"], result["retries"]), ("ok", 2, 1))
        self.assertIn("http_429", result["errors"][0])

    def test_no_data_and_unexpected_schema_recover_through_fallback(self):
        self.assertIn("no_data", ui_failure_spec("no_data", "telemetry"))
        for fault, expected in (("no_data", "no_data"), ("schema", "unexpected_schema")):
            spec = f"telemetry={fault}+{fault}+{fault}"
            result = Scout(FakeBB(), build(spec)).collect({"tool": "telemetry"}, plan_for())
            self.assertEqual(result["status"], "degraded_fallback", fault)
            self.assertTrue(result["fallback_used"] and result["records"], fault)
            self.assertTrue(all(expected in error for error in result["errors"]), result["errors"])

    def test_recurring_pattern_groups_months_site_and_equipment_without_causation(self):
        plan = plan_for("Investigate recurring power incidents at Site A.")
        records = [
            {"id": f"INC-P{n}", "type": "incident", "site": "NOI-DC1",
             "time": f"2024-0{n}-10T10:00:00+05:30", "severity": "P2", "category": "power",
             "summary": "UPS-1 warning during utility fluctuation"}
            for n in (1, 2, 3)
        ]
        collected = {"incidents": {"status": "ok", "source": "test history", "records": records,
                                    "attempts": 1, "errors": [], "retries": 0, "fallback_used": False,
                                    "missing_fields": []}}
        result, _ = intelligence.analyze(plan, collected)
        recurring = [p for p in result["patterns"] if p["kind"] == "recurring_pattern"]
        self.assertTrue(any("2024-01" in p.get("dimensions", {}).get("months", []) for p in recurring))
        equipment = [p for p in result["patterns"] if p["kind"] == "equipment_recurrence"]
        self.assertTrue(equipment and "required" in equipment[0]["statement"].lower())

    def test_workflow_timeout_stops_safely_with_explanation(self):
        original = strategist.plan

        def slow_plan(question):
            time.sleep(0.03)
            return original(question)

        with mock.patch.object(orchestrator, "MAX_SECONDS", 0.01), \
                mock.patch.object(strategist, "plan", side_effect=slow_plan):
            result = orchestrator.Orchestrator(THERMAL_Q, fault_spec=NO_NET).run()
        self.assertEqual(result["status"], "Stopped")
        investigation = row(result["id"])
        self.assertIn("STOPPED SAFELY", investigation["error"])
        stopped = store.one("SELECT * FROM audit_logs WHERE investigation_id = ? AND action = 'stop'",
                            (result["id"],))
        self.assertIn("STOPPED SAFELY", stopped["output"])

    def test_audit_records_canonical_reliability_fields(self):
        result = orchestrator.Orchestrator(THERMAL_Q,
                                           fault_spec="telemetry=500+500+500," + NO_NET).run()
        logs = store.query("SELECT * FROM audit_logs WHERE investigation_id = ? ORDER BY id", (result["id"],))
        required = {"timestamp", "execution_id", "agent", "mode", "action", "tool", "status", "reason",
                    "duration_ms", "retry_count", "revision_count", "fallback_used"}
        self.assertTrue(logs and all(required <= set(log) for log in logs))
        tool_logs = [log for log in logs if log["role"] == "scout" and log["tool"]]
        self.assertTrue(tool_logs)
        self.assertTrue(all(log["execution_id"] == result["id"] for log in logs))
        self.assertTrue(any(log["request_json"] and log["response_status"] and log["validation"]
                            and log["normalized_result_json"] for log in tool_logs))
        self.assertGreaterEqual(max(log["retry_count"] for log in tool_logs), 2)
        self.assertGreaterEqual(max(log["revision_count"] for log in logs), 1)


class TestFinalMVPIntegration(unittest.TestCase):
    def test_exact_power_judge_flow_rejects_revises_approves_and_audits_why(self):
        initial_plan, _ = strategist.plan(JUDGE_Q)
        self.assertEqual((initial_plan["domain"], initial_plan["sites"]), ("power", ["NOI-DC1"]))
        self.assertEqual([t["tool"] for t in initial_plan["tasks"]], ["incidents", "weather"])
        self.assertTrue(all(t["reason"] for t in initial_plan["tasks"]))

        runner = orchestrator.Orchestrator(
            JUDGE_Q, fault_spec="incidents=500+timeout+malformed,weather=down")
        summary = runner.run()
        detail = server.investigation(summary["id"])
        result, verdict = detail["result"], detail["guardian"]
        self.assertEqual((summary["status"], summary["revisions"], verdict["verdict"]),
                         ("Awaiting Approval", 1, "APPROVED"))
        self.assertEqual(result["hypotheses"][0]["kind"], "power_quality")
        self.assertGreaterEqual(result["hypotheses"][0]["confidence"], 70)
        support_refs = {ref for item in result["hypotheses"][0]["supporting"] for ref in item["refs"]}
        self.assertIn("MNT-304", support_refs)
        ups_fault = next(h for h in result["hypotheses"] if h["kind"] == "ups_fault")
        self.assertTrue(ups_fault["contradicting"])
        self.assertTrue(any(p["kind"] == "recurring_pattern" for p in result["patterns"]))
        self.assertIn("maintenance", [t["tool"] for t in detail["plan"]["tasks"]])

        incident_calls = [t for t in detail["tool_calls"] if t["tool"] == "incidents"]
        self.assertEqual([t["outcome"] for t in incident_calls[:4]],
                         ["http_500", "timeout", "malformed_response", "fallback_ok"])
        self.assertTrue(any(t["tool"].startswith("weather") for t in detail["tool_calls"]))
        guardian_rows = [a for a in detail["audit"] if a["role"] == "guardian" and a["action"] == "verify"]
        self.assertEqual([a["status"] for a in guardian_rows], ["rejected", "ok"])
        self.assertTrue(all(a["reason"] for a in detail["audit"]))

        gated = next(a for a in detail["actions"] if a["status"] == "Awaiting Approval")
        for field in ("reason", "priority_reason", "approval_reason", "expected_impact", "risk"):
            self.assertTrue(gated[field], field)
        self.assertEqual(gated["priority"], "High")
        self.assertIn("UPS", gated["recommended"])
        with self.assertRaisesRegex(ValueError, "rejection rationale"):
            orchestrator.decide_action(gated["id"], "reject", note="")
        approved = orchestrator.decide_action(
            gated["id"], "approve", actor="judge", note="Inspect upstream feed before replacing UPS hardware.")
        self.assertEqual((approved["status"], approved["ticket_ref"]),
                         ("Approved", f"MT-{gated['id']:04d}"))
        approval = store.one("SELECT * FROM approvals WHERE action_id = ?", (gated["id"],))
        self.assertEqual((approval["actor"], approval["note"]),
                         ("judge", "Inspect upstream feed before replacing UPS hardware."))
        self.assertTrue(store.one("SELECT 1 AS ok FROM audit_logs WHERE investigation_id = ? AND action = ?",
                                  (summary["id"], "approve_action")))

    def test_insufficient_evidence_is_explicit_and_guardian_rejects_vacuous_output(self):
        plan, _ = strategist.plan(JUDGE_Q)
        collected = {tool: {"status": "failed", "source": None, "records": [], "attempts": 4,
                            "errors": ["unavailable"], "retries": 2, "fallback_used": True,
                            "missing_fields": []} for tool in ("incidents", "weather")}
        result, ids = intelligence.analyze(plan, collected)
        summary, _ = intelligence.write_summary(result)
        verdict = guardian.review(result, summary, ids, collected)
        self.assertEqual(result["evidence_assessment"]["status"], "INSUFFICIENT EVIDENCE")
        self.assertFalse(result["evidence_assessment"]["known"])
        self.assertTrue(result["evidence_assessment"]["uncertain"])
        self.assertTrue(result["evidence_assessment"]["recommended_next_evidence"])
        self.assertEqual(verdict["verdict"], "REJECTED")
        self.assertIn("no matching evidence", verdict["reason"])

    def test_deterministic_benchmark_and_why_ui_contract(self):
        import benchmark
        results = benchmark.run_deterministic_benchmark(quiet=True)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r["passed"] for r in results))
        judge = next(r for r in results if r["scenario"] == "judge_power")
        self.assertEqual(judge["question"], JUDGE_Q)
        self.assertTrue(judge["recovered"] and judge["rejections"] and judge["revisions"])
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as f:
            app = f.read()
        for phrase in ("Why this investigation plan?", "Why selected", "Why this recommendation?",
                       "Why this priority?", "Why human approval?", "Why did the system do this?",
                       "Why this decision?", "Required revision", "INSUFFICIENT EVIDENCE"):
            self.assertIn(phrase, app)


    def test_readme_mvp_headings_and_required_diagrams(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        with open(os.path.join(root, "ARCHITECTURE.md"), encoding="utf-8") as f:
            architecture = f.read()
        expected = ["## Problem", "## Solution", "## Why Multi-Agent?", "## Architecture", "## Four Agents",
                    "## Orchestrator", "## Shared Blackboard", "## Database", "## Real Tool/API",
                    "## Failure Recovery", "## Guardian & Critic", "## Bounded Execution", "## Human Approval",
                    "## Action & Opportunity Engine", "## Action Board", "## Individual Agent Mode",
                    "## Why / Explainability System", "## Audit Trail", "## Insufficient Evidence Handling",
                    "## Demo Scenario", "## Setup", "## Testing", "## Screenshots", "## Architecture Diagrams",
                    "## Known Limitations"]
        self.assertEqual([line for line in readme.splitlines() if line.startswith("## ")], expected)
        self.assertGreaterEqual(readme.count("```mermaid"), 8)
        for concept in ("Components", "Full-workflow sequence", "Individual agent", "Tool failure recovery",
                        "Guardian rejection/revision", "Human approval", "SQLite", "Explainability"):
            self.assertIn(concept.lower(), architecture.lower())


# Schema v1 exactly as it shipped before tool_calls was renamed (used to prove in-place migration).
V1_SCHEMA = """
CREATE TABLE investigations (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, question TEXT, title TEXT, site TEXT,
  priority TEXT, status TEXT, confidence INTEGER, faults TEXT, is_benchmark INTEGER DEFAULT 0, plan_json TEXT,
  result_json TEXT, guardian_json TEXT, budget_json TEXT, revisions INTEGER DEFAULT 0, error TEXT, created_at TEXT,
  updated_at TEXT);
CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, code TEXT, issue TEXT,
  recommended TEXT, priority TEXT, owner TEXT, effort TEXT, confidence INTEGER, evidence_json TEXT, reason TEXT,
  opportunity TEXT, status TEXT, approval_state TEXT, requires_approval INTEGER, action_type TEXT, ticket_ref TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER, tool TEXT, source TEXT,
  attempt INTEGER, outcome TEXT, error TEXT, duration_ms INTEGER, time TEXT);
CREATE TABLE approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, action_id INTEGER, decision TEXT, actor TEXT,
  note TEXT, ts TEXT);
INSERT INTO investigations (code, question, status) VALUES ('VX-00001', 'legacy question', 'Approved');
INSERT INTO actions (investigation_id, code, status) VALUES (1, 'VX-00001-A1', 'Approved');
INSERT INTO tool_calls (investigation_id, tool, source, attempt, outcome) VALUES (1, 'telemetry', 'Ops API', 1, 'ok');
INSERT INTO approvals (action_id, decision, actor) VALUES (1, 'approve', 'legacy-user');
"""

REQUIRED_TABLES = {"investigations", "incidents", "agent_runs", "evidence", "hypotheses", "recommendations",
                   "actions", "approvals", "tool_executions", "audit_logs", "benchmark_runs"}


class TestSQLitePersistence(unittest.TestCase):
    def test_schema_v2_has_every_required_table_and_seeded_incidents(self):
        info = store.info()
        self.assertEqual(info["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(info["journal_mode"], "wal")
        self.assertTrue(REQUIRED_TABLES <= set(info["tables"]))
        kinds = {r["name"]: r["type"] for r in store.query("SELECT name, type FROM sqlite_master")}
        self.assertEqual(kinds["tool_calls"], "view")  # legacy read alias, not a duplicate table
        self.assertEqual(kinds["tool_executions"], "table")
        snapshot_ids = {r["id"] for r in ops_api.load_snapshot()["incidents"]}
        stored = {r["incident_id"] for r in store.query("SELECT incident_id FROM incidents")}
        self.assertTrue(snapshot_ids <= stored)
        store.init(force=True)  # idempotent: re-running init neither fails nor duplicates the catalog
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM incidents")["n"], len(stored))

    def test_full_workflow_persists_every_entity_and_links_them(self):
        res = orchestrator.run_workflow(THERMAL_Q, fault_spec="telemetry=500+500+500," + NO_NET)
        inv = res["id"]
        count = lambda table: store.one(f"SELECT COUNT(*) AS n FROM {table} WHERE investigation_id = ?",
                                        (inv,))["n"]
        for table in ("events", "evidence", "hypotheses", "recommendations", "actions", "agent_runs",
                      "tool_executions", "audit_logs"):
            self.assertGreater(count(table), 0, table)
        incident = store.one("SELECT * FROM incidents WHERE last_seen_investigation_id = ?", (inv,))
        self.assertTrue(incident and incident["times_collected"] >= 1)
        recs = store.query("SELECT * FROM recommendations WHERE investigation_id = ? ORDER BY rank", (inv,))
        self.assertTrue(all(r["guardian_verdict"] == "APPROVED" and r["verified_by_id"] == inv for r in recs))
        acts = store.query("SELECT * FROM actions WHERE investigation_id = ? ORDER BY id", (inv,))
        self.assertEqual([a["recommendation_id"] for a in acts], [r["id"] for r in recs])
        tools = store.query("SELECT * FROM tool_executions WHERE investigation_id = ?", (inv,))
        self.assertTrue(any(t["outcome"] == "fallback_ok" and t["fallback_used"] and t["records_count"]
                            for t in tools))
        self.assertTrue(all(t["mode"] == "FULL_WORKFLOW" and t["validation"] for t in tools))

        gated = next(a for a in acts if a["status"] == "Awaiting Approval")
        orchestrator.decide_action(gated["id"], "approve", actor="db-tester", note="looks right")
        approval = store.one("SELECT * FROM approvals WHERE action_id = ?", (gated["id"],))
        self.assertEqual((approval["investigation_id"], approval["previous_status"], approval["new_status"],
                          approval["ticket_ref"], approval["actor"]),
                         (inv, "Awaiting Approval", "Approved", f"MT-{gated['id']:04d}", "db-tester"))
        detail = server.investigation(inv)
        self.assertTrue(detail["recommendations"] and detail["approvals"] and detail["tool_calls"])
        linked = next(i for i in server.incidents() if i["id"] == incident["incident_id"])
        self.assertIn(inv, [x["id"] for x in linked["investigations"]])

    def test_approval_is_atomic_and_rolls_back_on_failure(self):
        res = orchestrator.run_workflow(THERMAL_Q, fault_spec=NO_NET)
        gated = store.one("SELECT * FROM actions WHERE investigation_id = ? AND status = 'Awaiting Approval'",
                          (res["id"],))
        audit_before = store.one("SELECT COUNT(*) AS n FROM audit_logs WHERE investigation_id = ?",
                                 (res["id"],))["n"]
        with mock.patch.object(orchestrator, "_refresh_investigation", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                orchestrator.decide_action(gated["id"], "approve")
        after = store.one("SELECT * FROM actions WHERE id = ?", (gated["id"],))
        self.assertEqual((after["status"], after["ticket_ref"]), ("Awaiting Approval", None))
        self.assertFalse(store.query("SELECT 1 FROM approvals WHERE action_id = ?", (gated["id"],)))
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM audit_logs WHERE investigation_id = ?",
                                   (res["id"],))["n"], audit_before)
        done = orchestrator.decide_action(gated["id"], "reject", note="retry after rollback")
        self.assertEqual((done["status"], done["approval_state"]), ("Investigating", "rejected"))

    def test_individual_chain_persists_recommendations_verdicts_and_action_links(self):
        intel = orchestrator.run_agent("intelligence", "Find recurring patterns", {"records": POWER_RECORDS},
                                       failure="overclaim")
        self.assertTrue(store.query("SELECT 1 FROM hypotheses WHERE investigation_id = ?", (intel["id"],)))
        g1 = orchestrator.prepare_handoff(intel["id"], "guardian").run_agent()
        verdicts = {r["guardian_verdict"] for r in store.query(
            "SELECT guardian_verdict FROM recommendations WHERE investigation_id = ?", (intel["id"],))}
        self.assertEqual(verdicts, {"REJECTED"})
        rev = orchestrator.prepare_handoff(g1["id"], "revise").run_agent()
        g2 = orchestrator.prepare_handoff(rev["id"], "guardian").run_agent()
        rev_recs = {r["id"]: r for r in store.query("SELECT * FROM recommendations WHERE investigation_id = ?",
                                                     (rev["id"],))}
        self.assertTrue(rev_recs and all(r["guardian_verdict"] == "APPROVED" and r["verified_by_id"] == g2["id"]
                                         and r["revision"] == 1 for r in rev_recs.values()))
        acts = orchestrator.prepare_handoff(g2["id"], "action")
        self.assertTrue(acts and all(a["recommendation_id"] in rev_recs for a in acts))

    def test_benchmark_invocation_is_stored(self):
        import benchmark
        results = benchmark.run_benchmark(n=2, fault_rate=0.5, seed=11, quiet=True)
        run = store.one("SELECT * FROM benchmark_runs ORDER BY id DESC LIMIT 1")
        self.assertEqual((run["status"], run["n_runs"], run["completed_runs"]),
                         ("completed", 2, sum(r["completed"] for r in results)))
        linked = store.query("SELECT id FROM investigations WHERE benchmark_run_id = ? AND is_benchmark = 1",
                             (run["id"],))
        self.assertEqual(sorted(r["id"] for r in linked), sorted(r["investigation_id"] for r in results))
        self.assertEqual(server.analytics()["benchmark"]["latest"]["id"], run["id"])
        self.assertEqual(server.benchmarks()[0]["results"], results)

    def test_data_survives_a_process_restart(self):
        res = orchestrator.run_workflow(THERMAL_Q, fault_spec=NO_NET)
        gated = store.one("SELECT * FROM actions WHERE investigation_id = ? AND status = 'Awaiting Approval'",
                          (res["id"],))
        orchestrator.decide_action(gated["id"], "approve", actor="restart-tester")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import json, sys, store; i = int(sys.argv[1]); a = int(sys.argv[2]); print(json.dumps({"
                "'inv': store.one('SELECT code, status FROM investigations WHERE id = ?', (i,)), "
                "'action': store.one('SELECT status, ticket_ref FROM actions WHERE id = ?', (a,)), "
                "'approval': store.one('SELECT actor FROM approvals WHERE action_id = ?', (a,)), "
                "'recs': store.one('SELECT COUNT(*) AS n FROM recommendations WHERE investigation_id = ?', (i,))['n'], "
                "'audit': store.one('SELECT COUNT(*) AS n FROM audit_logs WHERE investigation_id = ?', (i,))['n']}))")
        import subprocess
        out = subprocess.run([sys.executable, "-c", code, str(res["id"]), str(gated["id"])], cwd=root,
                             env=os.environ.copy(), capture_output=True, text=True, timeout=60, check=True)
        fresh = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(fresh["inv"], {"code": res["code"], "status": row(res["id"])["status"]})
        self.assertEqual(fresh["action"], {"status": "Approved", "ticket_ref": f"MT-{gated['id']:04d}"})
        self.assertEqual(fresh["approval"], {"actor": "restart-tester"})
        self.assertGreater(fresh["recs"], 0)
        self.assertGreater(fresh["audit"], 0)

    def test_legacy_v1_database_migrates_in_place_without_data_loss(self):
        import sqlite3
        path = os.path.join(TMP, "legacy_v1.db")
        conn = sqlite3.connect(path)
        conn.executescript(V1_SCHEMA)
        conn.close()
        with mock.patch.object(store, "DB_PATH", path):
            store.init(force=True)
            store.init(force=True)  # second run is a no-op
            info = store.info()
            self.assertEqual(info["schema_version"], store.SCHEMA_VERSION)
            self.assertTrue(REQUIRED_TABLES <= set(info["tables"]))
            self.assertEqual(info["tables"]["tool_executions"], 1)
            self.assertEqual(store.one("SELECT outcome FROM tool_calls")["outcome"], "ok")  # view still reads
            self.assertEqual(store.one("SELECT code FROM investigations")["code"], "VX-00001")
            self.assertEqual(store.one("SELECT actor FROM approvals")["actor"], "legacy-user")
            cols = {r["name"] for r in store.query("PRAGMA table_info(actions)")}
            self.assertIn("recommendation_id", cols)
            self.assertIsNone(store.one("SELECT recommendation_id FROM actions")["recommendation_id"])


class TestBugfixRegressions(unittest.TestCase):
    """Each test reproduces a bug found in the review; it fails on the old code."""

    def gated(self, inv_id):
        return store.query("SELECT * FROM actions WHERE investigation_id = ? AND status = 'Awaiting Approval'",
                           (inv_id,))

    def test_rerun_never_destroys_human_decisions(self):
        res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=NO_NET).run()
        a = self.gated(res["id"])[0]
        orchestrator.decide_action(a["id"], "approve")
        self.assertTrue(orchestrator.rerun_blocker(res["id"]))
        orchestrator.Orchestrator(THERMAL_Q, investigation_id=res["id"], more_evidence=True, fault_spec=NO_NET).run()
        kept = store.one("SELECT * FROM actions WHERE id = ?", (a["id"],))
        self.assertEqual((kept["status"], kept["ticket_ref"]), ("Approved", f"MT-{a['id']:04d}"))
        self.assertEqual(row(res["id"])["status"], "Approved")  # refused re-run does not become "Error"
        self.assertIn("rerun_refused", audit_actions(res["id"]))

    def test_rerun_without_decisions_clears_stale_fields(self):
        res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=NO_NET).run()
        store.update("investigations", res["id"], error="old error")
        again = orchestrator.Orchestrator(THERMAL_Q, investigation_id=res["id"], more_evidence=True,
                                          fault_spec=NO_NET).run()
        self.assertIsNone(row(res["id"])["error"])
        codes = [a["code"] for a in store.query("SELECT code FROM actions WHERE investigation_id = ?", (res["id"],))]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIn(again["status"], ("Awaiting Approval", "Approved"))

    def test_one_recommendation_yields_one_action_set(self):
        intel = orchestrator.run_agent("intelligence", "x", {"records": POWER_RECORDS})
        g1 = orchestrator.prepare_handoff(intel["id"], "guardian").run_agent()
        g2 = orchestrator.prepare_handoff(intel["id"], "guardian").run_agent()
        orchestrator.prepare_handoff(g1["id"], "action")
        self.assertEqual(orchestrator.available_handoffs(row(g2["id"])), [])
        with self.assertRaises(ValueError):
            orchestrator.prepare_handoff(g2["id"], "action")

    def test_partial_rejection_can_complete(self):
        res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=NO_NET).run()
        gated = self.gated(res["id"])
        store.execute("UPDATE actions SET status = 'Awaiting Approval', approval_state = 'pending', "
                      "requires_approval = 1 WHERE investigation_id = ?", (res["id"],))
        acts = self.gated(res["id"])
        self.assertGreaterEqual(len(acts), 2, gated)
        orchestrator.decide_action(acts[0]["id"], "reject", note="not needed")
        for a in acts[1:]:
            orchestrator.decide_action(a["id"], "approve")
            orchestrator.complete_action(a["id"])
        self.assertEqual(row(res["id"])["status"], "Completed")

    def test_rejected_workflow_actions_are_not_pending(self):
        spec = "intelligence=hallucinate+hallucinate+hallucinate+hallucinate+hallucinate," + NO_NET
        with mock.patch.object(orchestrator, "MAX_REVISIONS", 1):
            res = orchestrator.Orchestrator(THERMAL_Q, fault_spec=spec).run()
        states = {a["approval_state"] for a in store.query(
            "SELECT approval_state FROM actions WHERE investigation_id = ? AND requires_approval = 1", (res["id"],))}
        self.assertNotIn("pending", states)

    def test_timeout_worker_writes_nothing_after_deadline(self):
        with mock.patch.object(orchestrator, "AGENT_TIMEOUT_SECONDS", 0.2):
            res = orchestrator.run_agent("scout", "Fetch incidents", {"preferred_source": "incidents"},
                                         failure="timeout")
        self.assertEqual(res["status"], "Timeout")
        time.sleep(1.8)  # abandoned worker finishes its retries + fallback
        self.assertEqual(row(res["id"])["status"], "Timeout")
        self.assertFalse(store.query("SELECT 1 FROM events WHERE investigation_id = ?", (res["id"],)))
        calls_after = store.query("SELECT 1 FROM tool_executions WHERE investigation_id = ? AND outcome = "
                                  "'fallback_ok'", (res["id"],))
        self.assertFalse(calls_after)

    def test_zero_limits_do_not_crash(self):
        from blackboard import Blackboard
        self.assertEqual(Blackboard(1, "x", 0, 0, 0).summary()["budget_used_pct"], 0)

    def test_strict_dates(self):
        res = orchestrator.run_agent("scout", "incidents Site A", {"preferred_source": "incidents",
                                                                   "start": "2024-10-01", "end": "2024-9-30"})
        self.assertEqual(res["status"], "Validation Failed")
        self.assertEqual(strategist._detect_window("alarms on 2024-06-31"), ops_api.dataset_window())

    def test_guardian_text_review_edge_cases(self):
        chk = lambda v, n: next(c["passed"] for c in v["checks"] if c["check"] == n)  # noqa: E731
        self.assertEqual(guardian.review_text("The UPS may be involved [INC-9999].", "")["verdict"], "REJECTED")
        self.assertFalse(chk(guardian.review_text("Replace the UPS tonight without approval.", "UPS alarm 10:05"),
                             "recommendation_safety"))
        self.assertFalse(chk(guardian.review_text("Schedule UPS replacement tonight.", "UPS alarm 10:05"),
                             "recommendation_safety"))
        self.assertTrue(chk(guardian.review_text("Replace the UPS after human approval.", "UPS alarm 10:05"),
                            "recommendation_safety"))
        self.assertTrue(chk(guardian.review_text("Inlet temperature improves once the filter is cleaned.",
                                                 "CRAC alarm 10:05"), "no_overclaiming"))
        self.assertTrue(chk(guardian.review_text("A UPS fault is not proven; it may be the grid.",
                                                 "UPS alarm 10:05"), "no_overclaiming"))
        self.assertFalse(chk(guardian.review_text("The UPS may be involved [INC-1].", "INC-1001 UPS alarm 10:05"),
                             "citations_valid"))
        self.assertEqual(guardian.citations("[INC-9999; inc-1001]"), ["INC-9999", "INC-1001"])
        self.assertFalse(chk(guardian.review_text("I am 95.5% confident the UPS may be failing.", "UPS alarm 10:05"),
                             "confidence_justified"))
        # bare / parenthesised hallucinated IDs are caught in structured review too
        self.assertIn("INC-9999", guardian.mentioned_ids("see (INC-9999) and INC-1001"))

    def test_parse_records_edge_cases(self):
        self.assertEqual(len(intelligence.parse_records([{"time": "10:05", "summary": "UPS alarm"},
                                                         {"time": "10:07", "summary": "Server warning"}])[0]), 2)
        self.assertEqual(len(intelligence.parse_records("[10:05] UPS alarm\n[10:07] Server warning")[0]), 2)
        with self.assertRaises(intelligence.InputError):
            intelligence.parse_records("2024-13-01 10:05 UPS alarm\n2024-13-01 10:07 Server warning")
        ev, _ = intelligence.parse_records("10:30 Server warning\n10:20 UPS alarm\n10:10 Power fluctuation")
        self.assertEqual({e["dt"].day for e in ev}, {1})  # unsorted input is not spread over 3 days
        ev, _ = intelligence.parse_records("23:50 UPS alarm\n00:10 Server warning")
        self.assertEqual([e["dt"].day for e in ev], [1, 2])  # a real midnight rollover still works
        ev, skipped = intelligence.parse_records("10:05 UPS alarm\n10:99 Server warning\n10:07 Server warning")
        self.assertEqual((len(ev), skipped), (2, 1))
        import json
        ev, _ = intelligence.parse_records(json.dumps([{"time": "2024-06-10T10:05:00Z", "summary": "UPS alarm"},
                                                       {"time": "2024-06-10T10:35:00.250Z", "summary": "UPS alarm"}]))
        self.assertEqual({e["label"] for e in ev}, {"UPS alarm"})

    def test_keyword_matching_is_word_anchored(self):
        self.assertEqual(intelligence._component("Vendor follow-ups pending")["key"], "other")
        self.assertEqual(intelligence._component("Login attempt failed")["key"], "other")
        self.assertEqual(intelligence._component("Ports 3-4 flapping")["key"], "network")
        self.assertEqual(intelligence._component("UPS-G2 on battery")["key"], "ups")
        self.assertEqual(strategist._detect_domain("Why did the nightly backups fail?"), "general")
        self.assertEqual(strategist._detect_domain("UPS alarms at Site B"), "power")
        from agents.scout import choose_tools
        self.assertNotIn("incidents", choose_tools("Show telemetry to prevent overheating"))

    def test_scout_deadline_not_consumed_by_other_calls(self):
        import threading
        from agents import scout as scout_mod
        gate = threading.Event()
        blockers = [threading.Thread(target=lambda: scout_mod._with_timeout(lambda t: gate.wait(), 5))
                    for _ in range(10)]
        for b in blockers:
            b.start()
        self.assertEqual(scout_mod._with_timeout(lambda t: "ok", 0.5), "ok")
        gate.set()

    def test_telemetry_unit_preserved(self):
        for payload in (ops_api.ops_api_fetch("telemetry", FaultInjector(), 1),
                        ops_api.snapshot_fetch("telemetry", FaultInjector(), 1)):
            self.assertEqual(ops_api.normalize("telemetry", payload)[0]["unit"], "C")

    def test_telemetry_ids_are_unique_per_reading(self):
        from datetime import datetime
        a = ops_api.telemetry_id("NOI-DC1", "ROWB", datetime(2024, 6, 10, 10, 0, 5, tzinfo=ops_api.IST))
        b = ops_api.telemetry_id("NOI-DC1", "ROWB", datetime(2025, 6, 10, 10, 0, 55, tzinfo=ops_api.IST))
        self.assertNotEqual(a, b)
        recs = ops_api.normalize("telemetry", ops_api.snapshot_fetch("telemetry", FaultInjector(), 1))
        self.assertEqual(len({r["id"] for r in recs}), len(recs))

    def test_naive_timestamps_are_ist_not_host_local(self):
        self.assertEqual(ops_api.parse_time("2024-06-10T10:00:00"), ops_api.parse_time("2024-06-10T10:00:00+05:30"))
        self.assertEqual(ops_api.parse_time("1718000000"), ops_api.parse_time(1718000000))
        with self.assertRaises(ValueError):
            ops_api.parse_time(True)

    def test_weather_faults_apply_to_every_site(self):
        f = FaultInjector("weather=500+500+500")
        self.assertEqual([f.next("weather", scope="A") for _ in range(4)], ["500", "500", "500", None])
        self.assertEqual([f.next("weather", scope="B") for _ in range(3)], ["500", "500", "500"])
        down = FaultInjector("weather=down")
        self.assertEqual(down.next("weather", scope="B"), "down")
        plan = {"sites": ["NOI-DC1", "GGN-EDGE2"], "window": {"start": "2024-06-09", "end": "2024-06-14"}}
        res = Scout(FakeBB(), build("weather=500+500+500")).collect({"tool": "weather"}, plan)
        self.assertEqual((res["status"], res["fallback_used"], res["retries"]), ("degraded_fallback", True, 4))

    def test_llm_chain_falls_back_and_cools_down(self):
        import llm_client as L
        chain = [("omniroute:auto/best-free", "http://omni", "k1", "auto/best-free", 1),
                 ("gemini:flash-lite", "https://gem", "k2", "flash-lite", 1)]
        calls = []

        def fake_post(base, key, model, timeout, *a):
            calls.append(model)
            if base == "http://omni":
                raise RuntimeError("HTTP 503: Maximum combo retry limit reached")
            return "answer", 5, 2

        with mock.patch.object(L, "endpoints", return_value=chain), mock.patch.object(L, "_post_chat", fake_post), \
                mock.patch.object(L, "active_provider", return_value="openai_compatible"):
            L._cooldown.clear()
            text, meta = L.call_llm("s", "p", fallback="det")
            self.assertEqual((text, meta["provider"], meta["mock"]), ("answer", "gemini:flash-lite", False))
            self.assertIn("503", meta["fallback_from"][0])
            L.call_llm("s", "p", fallback="det")  # primary is cooling down: not called again
            self.assertEqual(calls, ["auto/best-free", "flash-lite", "flash-lite"])
            with mock.patch.object(L, "_post_chat", side_effect=RuntimeError("down")):
                L._cooldown.clear()
                text, meta = L.call_llm("s", "p", fallback="det")
            self.assertEqual((text, meta["mock"]), ("det", True))  # all providers down -> deterministic answer
            L._cooldown.clear()

    def test_unclosed_think_block_removed(self):
        import llm_client
        self.assertEqual(llm_client._clean("answer <THINK>internal reasoning cut off"), "answer")


if __name__ == "__main__":
    unittest.main()
