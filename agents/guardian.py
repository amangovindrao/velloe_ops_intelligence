"""
Velloe Guardian - "Can we trust this?"

Deterministic verifier (an LLM does not grade its own homework). Runs explicit
checks on the Intelligence result and returns APPROVED or REJECTED with reasons.
A rejection can carry `evidence_requests` (data sources the Scout should fetch)
so the orchestrator can run a bounded revision cycle.
"""
import re

CERTAINTY = ("definitely", "certainly", "confirmed root cause", "proves", "proven", "without doubt",
             "100%", "guaranteed")
ID_TOKEN = re.compile(r"^(INC|MNT|TEL|WX|EV)-[A-Za-z0-9\-]+$")
APPROVAL_MIN_CONFIDENCE = 60
# Corrective changes need confident evidence. Diagnostic work orders are how evidence is gathered,
# so they may be recommended at lower confidence (they still need human approval).
CORRECTIVE = ("maintenance_ticket", "change_review")


def citations(text):
    """Evidence IDs cited as [ID], [ID, ID] or [ID; ID] in any case (markdown links are ignored)."""
    out = []
    for inner in re.findall(r"\[([^\]]+)\](?!\()", text or ""):
        for tok in (t.strip().upper() for t in re.split(r"[,;]", inner)):
            if ID_TOKEN.match(tok):
                out.append(tok)
    return out


BARE_ID = re.compile(r"(?<![\w-])(?:INC|MNT|TEL|WX|EV)-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?![\w-])", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|un|isn't|wasn't|cannot|can't)\W*(?:\w+\W+)?$")


def mentioned_ids(text):
    """Every evidence-looking ID in the text, bracketed or bare, e.g. '(INC-9999)' or 'see INC-9999'."""
    return {m.group(0).upper() for m in BARE_ID.finditer(text or "")} | set(citations(text))


def _hits(terms, text):
    """Terms present as whole words/phrases and not directly negated ('not proven', 'unproven' excluded).
    Substring matching made 'improves' look like 'proves' and 'scheduled to' look like 'led to'."""
    found = []
    for term in terms:
        for m in re.finditer(rf"(?<![\w]){re.escape(term)}(?![\w])", text):
            if not NEGATION.search(text[max(0, m.start() - 14):m.start()]):
                found.append(term)
                break
    return found


def review(result, summary, evidence_ids, collected):
    checks, requests = [], []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    hyps, actions = result["hypotheses"], result["actions"]
    facts = result.get("facts") or []
    found_problem = bool(result["problem"]["refs"])

    # 1. structure and explicit fact/evidence/hypothesis/recommendation separation
    evidence = result.get("evidence") or []
    typed = (all(f.get("claim_type") == "FACT" and "confidence" not in f for f in facts)
             and all(e.get("claim_type") == "EVIDENCE" for e in evidence)
             and all(h.get("claim_type") == "HYPOTHESIS" for h in hyps)
             and all(a.get("claim_type") == "RECOMMENDATION" for a in actions))
    check("structure", result["problem"]["statement"] and facts and evidence and (hyps or not found_problem) and
          (actions or not found_problem), "facts, evidence, problem, hypotheses and recommendations present")
    check("evidence_sufficient", found_problem or bool(hyps),
          "matching evidence or a bounded hypothesis is present" if (found_problem or hyps)
          else "no matching evidence or hypothesis was found; human review or additional evidence is required")
    check("facts_separate", typed,
          "FACT, EVIDENCE, HYPOTHESIS and RECOMMENDATION records are explicitly separated")

    # 2. evidence validity - every referenced ID must exist in collected data
    referenced = set(result["problem"]["refs"]) | mentioned_ids(summary)
    for fact in facts:
        referenced |= set(fact.get("refs") or [])
    for item in evidence:
        referenced |= set(item.get("refs") or [])
    for h in hyps:
        for e in h["supporting"] + h["contradicting"]:
            referenced |= set(e["refs"])
    for a in actions:
        referenced |= set(a.get("evidence") or [])
    invalid = sorted(r for r in referenced if r not in evidence_ids)
    check("evidence_exists", not invalid, "all cited evidence IDs exist" if not invalid
          else f"cited evidence not found in collected data: {', '.join(invalid)}")

    # 3. unsupported claims
    unsupported = [h["title"] for h in hyps if h["confidence"] >= 40 and not h["supporting"]]
    check("hypotheses_supported", not unsupported, "every medium/high hypothesis has supporting evidence"
          if not unsupported else f"no supporting evidence for: {'; '.join(unsupported)}")
    overclaim = _hits(CERTAINTY, (summary or "").lower())
    check("no_overclaiming", not overclaim, "summary presents causes as hypotheses" if not overclaim
          else f"summary states speculation as fact ({', '.join(overclaim)})")
    low_summary = (summary or "").lower()
    causal = _hits(CAUSAL, low_summary)
    hedged = any(term in low_summary for term in ("hypothesis", "may", "might", "could", "possible", "suggest"))
    check("correlation_vs_causation", not causal or hedged,
          "causal language is absent or explicitly hedged as a hypothesis" if (not causal or hedged)
          else f"causal conclusion is presented without sufficient qualification ({', '.join(causal)})")
    check("summary_cited", not found_problem or citations(summary), "summary cites evidence")

    # 4. contradictions
    contradicted = [h["title"] for h in hyps if h["confidence"] >= 50 and
                    sum(e["strength"] == "strong" for e in h["contradicting"]) >
                    sum(e["strength"] == "strong" for e in h["supporting"])]
    check("no_contradictions", not contradicted, "no confident hypothesis outweighed by contradicting evidence"
          if not contradicted else f"contradicted but rated confident: {'; '.join(contradicted)}")

    # 5. recommendations
    weak_actions = [a["issue"] for a in actions if a.get("requires_approval") and a.get("type") in CORRECTIVE
                    and (a.get("confidence") or 0) < APPROVAL_MIN_CONFIDENCE]
    check("actions_justified", not weak_actions, "corrective actions rest on >= "
          f"{APPROVAL_MIN_CONFIDENCE}% confidence" if not weak_actions
          else f"operational action on weak evidence: {'; '.join(weak_actions)}")
    incomplete = [a["issue"] for a in actions if not (a.get("owner") and a.get("effort") and a.get("priority"))]
    check("actions_complete", not incomplete, "every action has owner, effort and priority")
    unsafe = [a["issue"] for a in actions if any(term in a.get("recommended", "").lower() for term in RISKY)
              and not a.get("requires_approval")]
    check("recommendation_safety", not unsafe,
          "disruptive recommendations require human approval" if not unsafe
          else f"disruptive recommendation bypasses human approval: {'; '.join(unsafe)}")

    # 6. missing evidence that the system could still obtain for the leading hypothesis
    if hyps:
        for m in hyps[0]["missing"]:
            if m.get("tool") and m.get("critical") and m["tool"] not in collected:
                requests.append(m["tool"])
    check("key_evidence_collected", not requests, "no obtainable key evidence missing" if not requests
          else f"leading hypothesis lacks obtainable evidence: {', '.join(requests)}")

    # 7. degraded sources must be disclosed
    degraded = [t for t, c in collected.items() if c["status"] != "ok"]
    disclosed = " ".join(u["what"] for u in result["unknowns"])
    undisclosed = [t for t in degraded if t not in disclosed]
    check("gaps_disclosed", not undisclosed, "degraded/failed sources are disclosed" if not undisclosed
          else f"not disclosed: {', '.join(undisclosed)}")
    field_gap_tools = [t for t, c in collected.items() if c.get("missing_fields") and c["status"] != "failed"]
    undisclosed_fields = [t for t in field_gap_tools if t not in disclosed]
    check("missing_fields_disclosed", not undisclosed_fields,
          "normalized record field gaps are disclosed" if not undisclosed_fields
          else f"missing normalized fields not disclosed: {', '.join(undisclosed_fields)}")

    failed = [c for c in checks if not c["passed"]]
    verdict = "APPROVED" if not failed else "REJECTED"
    base = {"verdict": verdict, "reasons": [c["detail"] for c in failed], "checks": checks,
            "evidence_requests": sorted(set(requests))}
    operator = explain(base, result)
    return base | operator | {"issues": [{"check": c["check"], "reason": c["detail"]} for c in failed]}


def explain(verdict, result):
    """Operator-facing fields for a structured review (individual mode)."""
    failed = [c for c in verdict["checks"] if not c["passed"]]
    missing = [m["what"] for h in result["hypotheses"][:1] for m in h["missing"]]
    revision = []
    for c in failed:
        revision.append({
            "no_overclaiming": "State the cause as a hypothesis with its confidence, not as a confirmed root cause.",
            "correlation_vs_causation": "Replace causal certainty with a correlation statement and list direct diagnostic evidence needed.",
            "evidence_exists": "Remove or correct citations that do not exist in the collected data.",
            "evidence_sufficient": "Collect matching operational evidence before approving a conclusion or action.",
            "facts_separate": "Label observations, evidence, hypotheses, and recommendations explicitly.",
            "summary_cited": "Cite the evidence IDs that support the summary.",
            "hypotheses_supported": "Drop or downgrade hypotheses that have no supporting evidence.",
            "no_contradictions": "Lower the confidence of hypotheses outweighed by contradicting evidence.",
            "actions_justified": "Replace the corrective action with a diagnostic step until confidence is higher.",
            "recommendation_safety": "Require human approval for any disruptive operational action.",
            "gaps_disclosed": "Disclose the degraded or failed data sources.",
            "key_evidence_collected": "Collect the missing evidence before concluding.",
        }.get(c["check"], c["detail"]))
    return {
        "reason": failed[0]["detail"] if failed else "All checks passed; claims are supported and hedged.",
        "unsupported_claims": [c["detail"] for c in failed if c["check"] in
                               ("no_overclaiming", "evidence_exists", "hypotheses_supported", "summary_cited")],
        "missing_evidence": missing,
        "risk": ("Acting on this result could direct work at the wrong component or leave the real cause "
                 "unresolved." if failed else "Low: recommendations are hedged and approval-gated."),
        "required_revision": revision,
    }


# ---------------------------------------------------------------------- free-text review (individual mode)
CAUSAL = ("because", "caused", "causes", "due to", "root cause", "is defective", "is faulty", "has failed",
          "is broken", "responsible for", "led to", "resulted in", "is the cause")
TEMPORAL = ("after", "before", "followed", "following", "happened", "coincided", "at the same time",
            "preceded", "when", "then")
MECHANISM = ("diagnostic", "log shows", "logs show", "measured", "measurement", "self-test", "test result",
             "confirmed by", "telemetry shows", "inspection found", "error code")
RISKY = ("replace", "shut down", "shutdown", "decommission", "disable", "power off", "roll back", "rollback",
         "restart", "reboot", "bypass")
HEALTHY = ("passed", "normal", "healthy", "no fault", "no faults", "no error", "no errors", "ok", "within range")
# Inflected forms too ('replacement', 'restarting', 'rolling back') - exact words missed them.
RISKY_RX = re.compile(r"\b(?:replac\w*|shut\s?down\w*|shutting down|decommission\w*|disabl\w*|power(?:ed|ing)? off|"
                      r"roll(?:ed|ing)? back|rollback\w*|restart\w*|reboot\w*|bypass\w*)")
APPROVAL_OK = re.compile(r"\b(?:after|with|pending|subject to|requires?|required|once|upon|following)\s+"
                         r"(?:explicit\s+|human\s+|change\s+|management\s+)*approv|\bapproved by\b|"
                         r"\bhuman approval\b")
APPROVAL_BYPASS = re.compile(r"\b(?:without|no|skip\w*|bypass\w*|before)\s+(?:any\s+|explicit\s+|human\s+)*approv")
LIMITS = ("fallback", "degraded", "stale", "unverified", "simulated", "partial", "incomplete")
DISCLOSE = ("limited", "limitation", "degraded", "fallback", "incomplete", "unverified", "caveat")
COMPONENT_EVIDENCE = [
    (r"(?<![\w-])ups(?!\w)", "UPS", ["UPS diagnostic logs", "Input voltage measurements",
                                          "Power-quality telemetry"]),
    (r"\bpower|\bvoltage|\bgrid\b|\bfluctuat", "power supply",
     ["Power-quality telemetry (voltage, frequency)", "Utility / grid event records"]),
    (r"\bcrac|\bhvac|\bcooling|\bchiller", "cooling system",
     ["HVAC/CRAC controller telemetry", "Cooling maintenance records", "Rack inlet temperature telemetry"]),
    (r"\bswitch(?:es)?\b|\bnetwork|\brouters?\b|\blinks?\b", "network equipment",
     ["Device syslog", "Interface error counters", "Change records for the device"]),
    (r"\bservers?\b|\bhosts?\b", "server", ["Server hardware / BMC logs", "Server power and temperature telemetry"]),
    (r"\bfirmware|\bupgrade|\bpatch(?:es|ed|ing)?\b|\bchange(?:s|d)?\b", "change",
     ["Change records with timestamps", "Rollback test results"]),
]
NUMBER = {"one": 1, "once": 1, "single": 1, "two": 2, "twice": 2, "three": 3, "a couple of": 2}


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def review_text(report, evidence=""):
    """Verify a free-text conclusion. Returns the same verdict shape as review() plus operator fields."""
    rep, ev = (report or "").strip(), (evidence or "").strip()
    low, evl = rep.lower(), ev.lower()
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    comps = [(name, need) for rx, name, need in COMPONENT_EVIDENCE if re.search(rx, low)]
    main = comps[0][0] if comps else "suspected component"
    cited = citations(rep)
    ev_lines = [l for l in ev.splitlines() if l.strip()]

    # A bracketed ID is a claim about evidence, not evidence: without supplied evidence it cannot be checked.
    check("evidence_provided", ev, "evidence supplied" if ev
          else "no evidence was supplied with the conclusion" + (" (cited IDs cannot be verified)" if cited else ""))
    certainty = _hits(CERTAINTY, low)
    check("no_overclaiming", not certainty, "claims are hedged" if not certainty
          else f"states speculation as fact ({', '.join(certainty)})")
    causal = _hits(CAUSAL, low)
    temporal = bool(_hits(TEMPORAL, low) or _hits(TEMPORAL, evl))
    mechanism = bool(_hits(MECHANISM, low) or _hits(MECHANISM, evl))
    corr = bool(causal) and temporal and not mechanism
    check("correlation_vs_causation", not corr, "no causal claim rests on timing alone" if not corr else
          f"The available evidence shows correlation (event timing) but does not establish that the {main} "
          "itself caused the incident.")
    direct_defect = bool(_hits(("is defective", "is faulty", "has failed", "is broken"), low))
    check("direct_diagnostic_support", not direct_defect or mechanism,
          "component defect claims have direct diagnostic support" if (not direct_defect or mechanism)
          else f"The claim that the {main} is defective has only correlated warnings; direct diagnostics are missing.")
    # "<n> [up to two words, but not 'of <n>'] <unit>" - so 'one of 40 alerts' is not read as 1 and
    # '3,000 alerts' is read as 3000, not 0.
    counts = [NUMBER.get(m.group(1), int(m.group(1).replace(",", "")) if m.group(1)[0].isdigit() else 99)
              for m in re.finditer(
                  r"(?<![\w,])(one|once|single|two|twice|three|a couple of|\d{1,3}(?:,\d{3})+|\d+)\b\s+"
                  r"(?:(?!of\b)[a-z]+\s+){0,2}?"
                  r"(?:times|occurrences|events|incidents|fluctuations|warnings|alerts|outages)", low)]
    small = bool(causal) and counts and min(counts) <= 3
    check("sufficient_sample", not small, "sample size not used to claim causation" if not small
          else f"only {min(counts)} occurrence(s) observed - too few to establish a cause")
    ev_ids = mentioned_ids(ev)
    bad_refs = [c for c in cited if ev and c not in ev_ids]  # exact IDs: 'INC-1' must not match 'INC-1001'
    check("citations_valid", not bad_refs, "cited records appear in the evidence" if not bad_refs
          else f"cited records not found in the evidence: {', '.join(bad_refs)}")
    contra = [l.strip() for l in ev_lines for rx, name, _ in COMPONENT_EVIDENCE
              if name == main and re.search(rx, l.lower()) and _hits(HEALTHY, l.lower())]
    check("no_contradictions", not contra, "no contradicting evidence found" if not contra
          else f"evidence contradicts the claim: {contra[0][:120]}")
    risky = sorted({m.group(0) for m in RISKY_RX.finditer(low)})
    gated = APPROVAL_OK.search(low) and not APPROVAL_BYPASS.search(low)
    unsafe = risky and not gated
    check("recommendation_safety", not unsafe, "no disruptive action without approval" if not unsafe
          else f"recommends a disruptive action ({', '.join(risky)}) without human approval")
    pct = [float(p) for p in re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", rep)]
    unjust = pct and max(pct) >= 70 and len(ev_lines) + len(cited) < 3
    check("confidence_justified", not unjust, "stated confidence is proportionate" if not unjust
          else f"{max(pct):g}% confidence stated with fewer than 3 pieces of evidence")
    weak_src = _hits(LIMITS, evl)
    undisclosed = weak_src and not _hits(DISCLOSE, low)
    check("source_reliability", not undisclosed, "source limitations disclosed or none found" if not undisclosed
          else f"evidence comes from {', '.join(weak_src)} sources but the report does not disclose it")

    failed = [c for c in checks if not c["passed"]]
    missing, seen = [], set()
    for _, need in comps:
        for n in need:
            base = n.split(" (")[0].lower()
            if base not in evl and base not in seen:
                seen.add(base)
                missing.append(n)
    revision = []
    names = {c["check"] for c in failed}
    if names & {"no_overclaiming", "correlation_vs_causation", "sufficient_sample", "direct_diagnostic_support"}:
        revision.append(f"State the {main} issue as a hypothesis rather than a confirmed root cause.")
    if missing and failed:
        revision.append("List the evidence required to confirm it: " + ", ".join(dict.fromkeys(missing)) + ".")
    if "recommendation_safety" in names:
        revision.append("Route the disruptive action through human approval, after diagnostics.")
    if "evidence_provided" in names:
        revision.append("Attach the records or measurements the conclusion is based on.")
    if "no_contradictions" in names:
        revision.append("Address the contradicting evidence or withdraw the claim.")
    if "source_reliability" in names:
        revision.append("Disclose that some evidence came from degraded or unverified sources.")
    if "confidence_justified" in names:
        revision.append("Lower the stated confidence or add supporting evidence.")
    if "citations_valid" in names:
        revision.append("Remove citations that are not in the supplied evidence.")
    verdict = "APPROVED" if not failed else "REJECTED"
    return {
        "verdict": verdict, "reasons": [c["detail"] for c in failed], "checks": checks, "evidence_requests": [],
        "reason": failed[0]["detail"] if failed else "The conclusion is hedged and consistent with the evidence.",
        "unsupported_claims": [s for s in _sentences(rep) if _hits(CERTAINTY + CAUSAL, s.lower())]
        if names & {"no_overclaiming", "correlation_vs_causation", "sufficient_sample", "direct_diagnostic_support"}
        else [],
        "missing_evidence": list(dict.fromkeys(missing)),
        "risk": ("Acting on an unverified root cause may lead to unnecessary replacement or leave the real cause "
                 "unresolved." if failed else "Low.")
        + (" The recommendation is disruptive." if "recommendation_safety" in names else ""),
        "required_revision": revision,
        "issues": [{"check": c["check"], "reason": c["detail"]} for c in failed],
    }
