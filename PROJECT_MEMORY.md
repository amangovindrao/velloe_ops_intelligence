# Project Memory

Final MVP handoff for Velloe Ops Intelligence, verified 2026-09-26.

## Current state

- Exactly four agents: Strategist, Scout, Intelligence, Guardian. Orchestrator is not an agent.
- Full workflow and Individual Agent Mode share Orchestrator validation, Blackboard budgets, persistence, policy, timeout, audit, and error handling.
- Genuine external tool: Open-Meteo Archive API; Ops records and all tickets are simulated.
- SQLite schema v2 is the only durable layer: investigations, incidents, events, agent runs, evidence, hypotheses, recommendations, actions, approvals, tool executions, audit logs, benchmark runs.
- Professional light-default/dark console includes Overview, Investigations, Action Board, Incidents, Agents, Audit, Analytics, Settings, responsive states, recovery banners, and focused WHY disclosures.

## Final verification

```text
69 tests passed in 122.099s
node --check web/app.js: PASS
deterministic benchmark: 8/8 scenarios passed
localhost application: all main assets/APIs HTTP 200
```

Exact HTTP flow: Site A power question → incidents API 500 retry limit → snapshot fallback → recurring power pattern → upstream-power hypothesis → Guardian rejection → maintenance evidence `MNT-304` → revision → Guardian approval at 70% evidence score → High action → human rationale → simulated ticket → audit. The prepared default CLI run is `VX-00009` with simulated ticket `MT-0026`.

Individual HTTP flow: supplied power records → Individual Intelligence Completed → explicit Guardian handoff → APPROVED. No silent agent chaining.

## Check-first resolution

| Area | Initial state | Final resolution |
|---|---|---|
| Core agents/orchestration/Blackboard | Complete | Reused and regression-tested |
| Exact Site A power demo | Broken | Added simulated `INC-1050/51/52`, `MNT-304`, power-quality vs UPS-fault analysis, exact UI/CLI question |
| Real tool in demo | Partial | Power plan includes genuine Open-Meteo contextual collection; never used as causal proof |
| Guardian evidence revision | Partial for power path | Missing maintenance is obtainable evidence; first review rejects, bounded revision approves |
| WHY system | Partial data, incomplete UI | Reused reason/evidence contract; added selection, priority, approval, risk, impact, insufficient-evidence explanations |
| Insufficient evidence | Partial | Exact message, structured evidence assessment, Guardian vacuous-output rejection |
| Analytics | Partial | Added tool failure/recovery totals and stored deterministic scenario table |
| Benchmark | Random only | Added fixed 8-scenario acceptance matrix; random suite retained behind `--suite random` |
| Documentation | Accumulated/stale headings | README rebuilt with exact required MVP headings; architecture/memory/roadmap synchronized |

## Exact demo evidence

- `INC-1050`: utility input voltage sag; UPS-A1 transfers to battery.
- `INC-1051`: repeated utility undervoltage; UPS-A1 on battery and rack PDU alert (P1).
- `INC-1052`: utility undervoltage; UPS-A1 warning and downstream server alarms.
- `MNT-304`: UPS-A1 self-test passed; input logs recorded utility undervoltage; utility feed/ATS inspection requested.

The leading `power_quality` hypothesis receives 50% before diagnostic evidence and 70% after `MNT-304`. The internal `ups_fault` hypothesis is contradicted by utility-first evidence and a passed UPS self-test. Confidence is an evidence-weight score, not probability.

## WHY contract

No extra explanation agent/table was created. Existing fields are carried end-to-end:

- Strategist: `tasks[].reason`
- Scout: `selection_reason`, retry/fallback audit reasons, request/status/validation/provenance
- Intelligence: pattern statements/refs, supporting/contradicting/missing evidence, action `reason`, `priority_reason`, `approval_reason`, `expected_impact`, `risk`, `missing_evidence`
- Guardian: `reason`, `issues`, `missing_evidence`, `risk`, `required_revision`, checks
- Human: required rejection rationale; optional approval rationale; approvals + audit
- Orchestrator: revision and `STOPPED SAFELY` explanations

Actions/recommendations persist WHY fields in SQLite. UI presents them through compact details rather than duplicating every explanation on every screen.

## Deterministic benchmark

`python benchmark.py` runs: Normal, API failure/retry success, Timeout/fallback, Malformed/fallback, No data/fallback, Guardian rejection/revision, exact judge power, and Safe stopping. Results persist expected-vs-actual, completion, recovery, retries, fallback, rejection, revisions, stopping, and time. `python benchmark.py --suite random --n 6 --rate 0.5 --seed 7` retains the probabilistic benchmark.

## Important conventions

- Default DB: `output/velloe_ops.db`; WAL mode; `DB_PATH` override supported.
- `tool_calls` is a read-only compatibility view; durable table is `tool_executions`.
- Full IDs: `VX-xxxxx`; individual IDs: `EX-xxxxx`.
- Action states: Detected, Investigating, Awaiting Approval, Approved, Completed.
- Approval creates local simulated `MT-xxxx` only; one Intelligence recommendation can produce only one action set.
- Telemetry evidence IDs: `TEL-<site>-<sensor>-<YYYYMMDDHHMM[SS]>` (unique per reading).
- Timestamps without an offset are interpreted as IST (dataset zone), never host-local time.
- Fault specs are per endpoint; weather faults are applied per site (`faults.next("weather", scope=site)`).
- Exact CLI demo: `python run_demo.py --scenario judge --approve y`.

## Known limitations

- No authentication/RBAC; demo actor identities only; localhost deployment.
- Python caller deadlines cannot terminate running threads.
- Guardian is deterministic validation, not formal semantic entailment.
- Request More Evidence replaces derived rows on the same full investigation, so it is refused (HTTP 409, button disabled) once any action has a human decision; decisions and tickets are never overwritten.
- Only the newer recommendations table has an enforced foreign key; older links use indexed IDs.
- Thresholds/correlation windows are global.
- UI validation includes source/API contracts and live HTTP smoke, not browser screenshots or Playwright.
- No real screenshots are available; none were fabricated.

## Next work

1. Make Request More Evidence create an immutable child investigation.
2. Add authentication/RBAC and authenticated approver identity.
3. Add browser automation/visual regression at desktop, laptop, tablet, and phone widths.
4. Add real BMS/SNMP/Prometheus and ticketing connectors only behind the existing approval/audit controls.
