# Roadmap

Status after final MVP integration, verified 2026-09-26.

## MVP complete

- [x] Exactly four distinct agents plus non-agent Orchestrator
- [x] Real delegation through structured Strategist tasks and Guardian evidence requests
- [x] Shared Blackboard state, budgets, status, and audit
- [x] Genuine Open-Meteo API with real schema handling and cached fallback
- [x] 500, 429, timeout, connection, malformed, schema, no-data, retry-limit, fallback, and graceful failure paths
- [x] Structured FACT / EVIDENCE / HYPOTHESIS / RECOMMENDATION output
- [x] Site/equipment/time/incident/maintenance recurring patterns without causal overclaim
- [x] Guardian rejection, missing evidence, bounded revision, and approval
- [x] Explicit insufficient-evidence assessment and vacuous-output rejection
- [x] Human approval/rejection rationale, transactional decision, simulated local ticket, and audit
- [x] SQLite schema v2, additive migration, incident catalog, recommendations/actions links, benchmark persistence, restart survival
- [x] Individual Strategist, Scout, Intelligence, Guardian with explicit user handoffs and no silent chaining
- [x] Action & Opportunity Engine with reason, priority/approval rationale, impact, risk, and missing evidence
- [x] SQLite-backed Kanban/table Action Board
- [x] Composable Audit Trail and Analytics with demo/benchmark labels
- [x] Professional light/dark responsive operations console and focused WHY disclosures
- [x] Exact Site A power judge scenario in UI, CLI, tests, and deterministic benchmark
- [x] Deterministic 8-scenario benchmark plus retained random benchmark mode
- [x] README exact MVP sections and eight required Mermaid concepts; architecture/memory synchronized
- [x] 69 tests passing; JavaScript syntax and live localhost application/API smoke passing

## Next: audit-history integrity

| Priority | Item | Acceptance condition | Effort |
|---|---|---|---|
| High | Immutable child investigation for Request More Evidence | Original result/actions/approvals remain unchanged and linked to child | Medium |
| High | Lifecycle tests for parent/child evidence runs | API/UI/audit prove immutable lineage | Medium |
| High | Export authoritative post-approval trace | Human decision/action appears in a signed or regenerated trace snapshot | Medium |

## Productization

| Priority | Item | Effort |
|---|---|---|
| High | Authentication/RBAC and authenticated approvers | Medium |
| High | Real BMS/HVAC, SNMP, Prometheus, incident connectors | High |
| High | Jira/ServiceNow integration with idempotency behind approval | Medium |
| High | Cancellable process/async tool execution | High |
| Medium | Browser automation and visual regression at four widths/themes | Medium |
| Medium | Claim-level semantic evidence-entailment evaluation | Medium |
| Medium | Per-site/per-metric thresholds and historical windows | Medium |
| Medium | Labelled evaluation set and confidence calibration | Medium |
| Low | Signed PDF/JSON investigation export | Low |

## Verified release gates

1. Exact power demo reaches failure, retries, fallback, pattern, competing hypotheses, Guardian reject/revise/approve, approval-gated High action, human rationale, simulated ticket, and audit.
2. Individual Intelligence analyzes supplied records and hands off explicitly to Guardian.
3. Deterministic benchmark passes 8/8 named scenarios without manipulated results.
4. Test loader discovers and passes 69 tests.
5. `node --check web/app.js` passes.
6. Fresh localhost server returns HTTP 200 for all primary UI/API endpoints.
7. README and Architecture contain system, full workflow, agent interaction, recovery, Guardian revision, individual/full, approval/database, and WHY diagrams.

## Explicitly out of scope

- Autonomous or real infrastructure changes
- Production statistics from simulated/benchmark data
- Fabricated operational evidence
- Claiming correlation proves causation
- Public deployment without authentication
- Claiming thread deadlines kill underlying work
- Fake screenshots
