"""
CLI for Velloe Ops Intelligence (the web dashboard is server.py).

  python run_demo.py --question "Why are repeated power fluctuations happening at Site A, and what should we do?" --scenario judge --approve y
  python run_demo.py --scenario demo --approve y
"""
import argparse
import os
import sys

import store
from config import OUTPUT_DIR
from llm_client import active_provider
from orchestrator import Orchestrator, decide_action
from tools.faults import SCENARIOS, UI_FAILURES


def report_markdown(inv, actions):
    r = store.loads(inv["result_json"], {})
    g = store.loads(inv["guardian_json"], {})
    lines = [f"# {inv['code']} - {inv['title']}", "",
             f"_Velloe Ops Intelligence - Demo / Simulated Data_  ",
             f"**Status:** {inv['status']} | **Priority:** {inv['priority']} | **Confidence:** {inv['confidence']}% "
             f"| **Guardian:** {g.get('verdict')} after {inv['revisions']} revision(s)", "",
             "## Summary", r.get("summary", ""), "",
             "## Problem", r["problem"]["statement"], "", "## Impact", r["impact"]["statement"], "",
             "## Root-cause hypotheses"]
    for h in r["hypotheses"]:
        lines.append(f"### {h['id']}. {h['title']} - {h['confidence']}% ({h['label']})")
        lines += [f"- ✓ {e['statement']} [{', '.join(e['refs'])}] ({e['strength']})" for e in h["supporting"]]
        lines += [f"- ✗ {e['statement']} [{', '.join(e['refs'])}] ({e['strength']})" for e in h["contradicting"]]
        lines += [f"- ⚠ Missing: {m['what']}" for m in h["missing"]]
        lines.append("")
    lines += ["## Action board", "", "| Code | Issue | Priority | Owner | Effort | Status | Recommended |",
              "|---|---|---|---|---|---|---|"]
    lines += [f"| {a['code']} | {a['issue']} | {a['priority']} | {a['owner']} | {a['effort']} | {a['status']}"
              f"{' (' + a['ticket_ref'] + ')' if a['ticket_ref'] else ''} | {a['recommended']} |" for a in actions]
    lines += ["", "## Business opportunities"] + [f"- {o}" for o in r.get("opportunities", [])]
    lines += ["", "## Unknowns / data limitations"] + [f"- {u['what']}" for u in r.get("unknowns", [])]
    lines += ["", "## Guardian checks"] + [f"- {'PASS' if c['passed'] else 'FAIL'} {c['check']}: {c['detail']}"
                                           for c in g.get("checks", [])]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description="Velloe Ops Intelligence - CLI demo")
    p.add_argument("--question", default="Why are repeated power fluctuations happening at Site A, and what should we do?")
    p.add_argument("--failure", choices=list(UI_FAILURES), default="normal",
                   help="dashboard-style failure simulation on the first data source")
    p.add_argument("--scenario", choices=list(SCENARIOS), help="named multi-fault scenario (overrides --failure)")
    p.add_argument("--approve", choices=["y", "n"], help="answer the human approval gate without prompting")
    args = p.parse_args()

    spec = SCENARIOS[args.scenario] if args.scenario else None
    orch = Orchestrator(args.question, failure=args.failure, fault_spec=spec)
    print(f"Running {orch._code()} (LLM provider: {active_provider()}) ...")
    res = orch.run()
    for e in orch.bb.trace:
        print(f"  t+{e['t']:>5}s  {e['agent']:<20} {e['action']:<24} [{e['status']}] {str(e['output'])[:70]}")

    inv = store.one("SELECT * FROM investigations WHERE id = ?", (res["id"],))
    pending = store.query("SELECT * FROM actions WHERE investigation_id = ? AND status = 'Awaiting Approval'",
                          (res["id"],))
    for a in pending:
        print(f"\nHUMAN APPROVAL REQUIRED\n  {a['recommended']}\n  Priority: {a['priority']}  Owner: {a['owner']}"
              f"  Confidence: {a['confidence']}%\n  Reason: {a['reason']}")
        ans = args.approve or (input("Approve this action? [y/N]: ").strip().lower() if sys.stdin.isatty() else "n")
        a2 = decide_action(a["id"], "approve" if ans == "y" else "reject", actor="cli-user",
                           note="CLI operator approved the recommendation." if ans == "y" else
                           "CLI operator declined the recommendation.")
        print(f"  -> {a2['status']}" + (f" (simulated ticket {a2['ticket_ref']} created)" if a2["ticket_ref"] else ""))

    inv = store.one("SELECT * FROM investigations WHERE id = ?", (res["id"],))
    actions = store.query("SELECT * FROM actions WHERE investigation_id = ?", (res["id"],))
    path = os.path.join(OUTPUT_DIR, f"{inv['code']}.md")
    if inv["result_json"]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(report_markdown(inv, actions))
    print(f"\n{inv['code']}: {inv['status']} | priority {inv['priority']} | confidence {inv['confidence']}% "
          f"| revisions {inv['revisions']}")
    print(f"Report: {path if inv['result_json'] else '(none)'}\nAudit trace: {os.path.join(OUTPUT_DIR, 'trace.json')}")
    print(f"Budget: {res['budget']}")


if __name__ == "__main__":
    main()
