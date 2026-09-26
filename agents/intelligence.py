"""
Velloe Intelligence - "What does this mean, why might it have happened, and what should we do?"

Combines analysis + investigation + the Action & Opportunity Engine:
  1. anomalies (telemetry above threshold, grouped into episodes)
  2. patterns and correlations (incidents vs telemetry vs maintenance vs weather)
  3. competing root-cause hypotheses scored from supporting / contradicting / missing evidence
  4. problem, impact, risks, unknowns
  5. prioritized actions with owner, effort, evidence and business opportunity

All numbers come from deterministic code over collected records; every claim
carries evidence IDs. The LLM (if configured) only words the executive summary,
and the Guardian checks that summary for invented citations or overclaiming.

Confidence = clamp(20 + 15*strong_support + 10*moderate_support
                   - 20*strong_contra - 10*moderate_contra - 5*missing, 5, 90), rounded to 5.
It is an evidence-weight score, not a probability.
"""
import re
from collections import Counter, defaultdict
from datetime import timedelta, timezone

from config import THERMAL_THRESHOLD_C
from llm_client import call_llm
from tools.ops_api import parse_time, site_registry

COOLING = ("CRAC", "HVAC", "AHU", "CHILLER", "COOL")
NETWORK = ("CSW", "SW", "RTR", "FW", "LB")
POWER = ("UPS", "PDU", "GEN", "ATS")
PERSISTENT_CHANGES = ("firmware_upgrade", "config_change", "software_update")
CORRELATION_WINDOW = timedelta(minutes=90)
PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
ACTION_CONFIDENCE = 60  # below this, recommend collecting more evidence instead of an operational action

SYSTEM = (
    "You are Velloe Intelligence, the analysis agent of an infrastructure operational-intelligence "
    "system. Write for an operations manager. Use ONLY the facts provided, cite evidence IDs in "
    "square brackets exactly as given, present causes as hypotheses with their confidence, and "
    "never claim certainty."
)


def asset_class(asset):
    a = (asset or "").upper()
    if a.startswith(COOLING):
        return "cooling"
    if a.startswith(POWER):
        return "power"
    if a.startswith(NETWORK):
        return "network"
    return "other"


def _fmt(dt):
    return dt.strftime("%d %b %H:%M")


def _ev(statement, strength, refs):
    return {"statement": statement, "strength": strength, "refs": list(dict.fromkeys(refs))}


def _score(h):
    s = 20
    s += sum(15 if e["strength"] == "strong" else 10 for e in h["supporting"])
    s -= sum(20 if e["strength"] == "strong" else 10 for e in h["contradicting"])
    s -= 5 * len(h["missing"])
    s = max(5, min(90, s))
    return int(round(s / 5.0) * 5)


def _label(conf):
    return "High" if conf >= 70 else "Medium" if conf >= 40 else "Low"


def _explain_actions(actions, p1=False, ongoing=False, impact=None, risks=None, unknowns=None):
    """Attach one reusable WHY contract to recommendations before persistence/UI rendering."""
    risks, unknowns = risks or [], unknowns or []
    missing = [u["what"] for u in unknowns if u.get("kind") in ("missing_evidence", "gap", "field_gap")]
    for action in actions:
        confidence = action.get("confidence")
        priority = action.get("priority")
        if priority == "Critical":
            priority_reason = "Critical because the event is ongoing, service-affecting, and confidence is at least 70%."
        elif priority == "High":
            basis = "a P1 service-affecting incident occurred" if p1 else \
                f"the leading evidence score is {confidence}%" if confidence is not None else "operational impact is high"
            priority_reason = f"High because {basis}; the event is {'ongoing' if ongoing else 'not currently ongoing'}."
        elif priority == "Medium":
            priority_reason = "Medium because investigation or evidence work is needed, but no ongoing critical impact is established."
        else:
            priority_reason = "Low because this is a preventive integration opportunity rather than an active incident response."
        action["priority_reason"] = priority_reason
        action["approval_reason"] = ("Human approval is required because this creates or changes tracked operational work."
                                     if action.get("requires_approval") else
                                     "Human approval is not required because this is read-only analysis or a non-disruptive task.")
        action["expected_impact"] = (impact or {}).get("statement") or "Impact remains to be confirmed."
        action["risk"] = risks[0]["risk"] if risks else "Risk is limited to acting on incomplete evidence."
        action["missing_evidence"] = list(dict.fromkeys(missing))
    return actions


class Analysis:
    def __init__(self, plan, collected):
        self.plan, self.collected = plan, collected
        self.registry = site_registry()
        self.domain = plan["domain"]
        by_tool = {t: c.get("records", []) for t, c in collected.items()}
        self.incidents = sorted(by_tool.get("incidents", []), key=lambda r: r["time"])
        self.telemetry = sorted(by_tool.get("telemetry", []), key=lambda r: (r["site"], r["sensor"], r["time"]))
        self.maintenance = sorted(by_tool.get("maintenance", []), key=lambda r: r["start"])
        self.weather = sorted(by_tool.get("weather", []), key=lambda r: (r["site"], r["date"]))
        self.evidence_ids = {r["id"] for recs in by_tool.values() for r in recs}
        self.have = {t for t, c in collected.items() if c["status"] != "failed"}
        self.attempted = set(collected)

    def site_label(self, site):
        return self.registry.get(site, {}).get("label", site)

    # ------------------------------------------------------------ 1. anomalies
    def episodes(self):
        eps, current, prev_key = [], None, None
        for r in self.telemetry:
            key = (r["site"], r["sensor"])
            if key != prev_key and current:
                eps.append(current)
                current = None
            prev_key = key
            if r["value"] > THERMAL_THRESHOLD_C:
                if current is None:
                    current = {"site": r["site"], "sensor": r["sensor"], "ids": [], "values": [], "times": []}
                current["ids"].append(r["id"])
                current["values"].append(r["value"])
                current["times"].append(parse_time(r["time"]))
            elif current:
                current["recovery_id"], current["recovery_time"] = r["id"], parse_time(r["time"])
                eps.append(current)
                current = None
        if current:
            eps.append(current)
        for e in eps:
            e["start"], e["end"] = e["times"][0], e["times"][-1]
            e["peak"] = max(e["values"])
            e["peak_id"] = e["ids"][e["values"].index(e["peak"])]
        return eps

    def reading_near(self, site, when):
        best = None
        for r in self.telemetry:
            if r["site"] != site:
                continue
            gap = abs(parse_time(r["time"]) - when)
            if gap <= CORRELATION_WINDOW and (best is None or gap < best[0]):
                best = (gap, r)
        return best[1] if best else None

    def domain_incidents(self):
        cats = {"thermal": ("thermal",), "network": ("network",), "power": ("power",)}.get(self.domain)
        rows = [i for i in self.incidents if not cats or i["category"] in cats]
        return rows or ([i for i in self.incidents if i["severity"] in ("P1", "P2")] if not cats else [])

    # ------------------------------------------------------------ main
    def run(self):
        eps = self.episodes()
        dom_inc = self.domain_incidents()
        hot_inc = []
        for i in self.incidents:
            r = self.reading_near(i["site"], parse_time(i["time"]))
            if r and r["value"] > THERMAL_THRESHOLD_C:
                hot_inc.append((i, r))

        # problem period
        if self.domain == "thermal" and eps:
            p_start, p_end = eps[0]["start"], eps[-1]["end"]
        elif dom_inc:
            p_start, p_end = parse_time(dom_inc[0]["time"]), parse_time(dom_inc[-1]["time"])
        elif eps:
            p_start, p_end = eps[0]["start"], eps[-1]["end"]
        else:
            p_start = p_end = None
        period_inc = [i for i in self.incidents if p_start and p_start - timedelta(hours=2)
                      <= parse_time(i["time"]) <= p_end + timedelta(hours=6)]

        patterns = self._patterns(eps, dom_inc, hot_inc)
        hypotheses = self._hypotheses(eps, dom_inc, hot_inc, p_start, p_end)
        unknowns = self._unknowns(hypotheses)
        problem = self._problem(eps, dom_inc, p_start, p_end)
        impact = self._impact(period_inc, eps)
        risks = self._risks(eps, period_inc, hypotheses)
        actions = self._actions(hypotheses, eps, hot_inc, unknowns, period_inc, p_end)
        window_end = parse_time(self.plan["window"]["end"] + "T23:59:00+05:30")
        ongoing = bool(p_end and window_end - p_end < timedelta(hours=6))
        _explain_actions(actions, p1=bool(impact["p1"]), ongoing=ongoing,
                         impact=impact, risks=risks, unknowns=unknowns)
        timeline = self._timeline(eps, period_inc, p_start, p_end)
        facts = [
            {"kind": "problem", "statement": problem["statement"], "refs": problem["refs"]},
            {"kind": "impact", "statement": impact["statement"], "refs": impact["refs"]},
        ] + [{"kind": p["kind"], "statement": p["statement"], "refs": p["refs"]} for p in patterns]
        for fact in facts:
            fact["claim_type"] = "FACT"
        for hypothesis in hypotheses:
            hypothesis["claim_type"] = "HYPOTHESIS"
        for action in actions:
            action["claim_type"] = "RECOMMENDATION"
        evidence = []
        for fact in facts:
            evidence.append({"claim_type": "EVIDENCE", "relation": "observed",
                             "statement": fact["statement"], "refs": fact["refs"]})
        for hypothesis in hypotheses:
            for relation in ("supporting", "contradicting"):
                for item in hypothesis[relation]:
                    evidence.append({"claim_type": "EVIDENCE", "hypothesis": hypothesis["id"],
                                     "relation": relation, **item})
        evidence_assessment = {
            "status": "SUFFICIENT FOR BOUNDED ANALYSIS" if self.evidence_ids and problem["refs"] else
                      "INSUFFICIENT EVIDENCE",
            "available_evidence": sorted(self.evidence_ids),
            "known": [fact["statement"] for fact in facts if fact.get("refs")],
            "uncertain": [item["what"] for item in unknowns],
            "recommended_next_evidence": [item["what"] for item in unknowns
                                          if item.get("kind") in ("missing_evidence", "gap", "field_gap")],
        }
        top = hypotheses[0] if hypotheses else None
        chain = sorted(top["chain"], key=lambda c: c["time"]) if top else []
        sites = sorted({e["site"] for e in eps} | {i["site"] for i in dom_inc}) or self.plan["sites"]
        return {
            "title": problem["title"],
            "site": ", ".join(self.site_label(s) for s in sites),
            "problem": problem,
            "impact": impact,
            "facts": facts,
            "evidence": evidence,
            "evidence_assessment": evidence_assessment,
            "patterns": patterns,
            "hypotheses": hypotheses,
            "evidence_chain": [{k: v for k, v in c.items() if k != "time"} | {"time": _fmt(c["time"])}
                               for c in chain],
            "unknowns": unknowns,
            "risks": risks,
            "actions": actions,
            "recommendations": actions,
            "opportunities": list(dict.fromkeys(a["opportunity"] for a in actions if a.get("opportunity"))),
            "timeline": timeline,
            "priority": min((a["priority"] for a in actions), key=PRIORITY_ORDER.get, default="Low"),
            "confidence": top["confidence"] if top else 0,
            "sources": {t: {"status": c["status"], "source": c["source"], "records": len(c["records"]),
                            "attempts": c["attempts"], "selection_reason": c.get("selection_reason"),
                            "fallback_used": c.get("fallback_used", False)} for t, c in self.collected.items()},
        }

    # ------------------------------------------------------------ 2. patterns
    def _patterns(self, eps, dom_inc, hot_inc):
        """Detect scoped recurrence and correlation without converting either into causation."""
        out = []
        episode_groups = defaultdict(list)
        for episode in eps:
            episode_groups[(episode["site"], episode["sensor"])].append(episode)
        for (site, sensor), group in sorted(episode_groups.items()):
            group.sort(key=lambda e: e["start"])
            peak = max(group, key=lambda e: e["peak"])
            out.append({"kind": "anomaly", "dimensions": {"site": site, "equipment": sensor}, "statement":
                        f"{self.site_label(site)} {sensor} inlet temperature exceeded {THERMAL_THRESHOLD_C:g} °C "
                        f"in {len(group)} period(s) between {_fmt(group[0]['start'])} and {_fmt(group[-1]['end'])}; "
                        f"peak {peak['peak']} °C.", "refs": [e["peak_id"] for e in group]})
            hours = Counter(t.hour for e in group for t in e["times"])
            day_share = sum(v for h, v in hours.items() if 11 <= h <= 17) / max(1, sum(hours.values()))
            if len(group) >= 2:
                out.append({"kind": "recurring_pattern", "dimensions": {"site": site, "equipment": sensor,
                            "timestamps": [_fmt(e["start"]) for e in group]},
                            "statement": f"Recurring Pattern Detected: {len(group)} thermal exceedance periods on "
                            f"{sensor} at {self.site_label(site)}; this is recurrence, not proof of a root cause.",
                            "refs": [e["peak_id"] for e in group]})
            if day_share >= 0.6:
                out.append({"kind": "pattern", "dimensions": {"site": site, "equipment": sensor,
                            "time_band": "11:00-17:00"}, "statement": f"{int(day_share * 100)}% of "
                            f"{self.site_label(site)} {sensor} over-threshold readings fall between 11:00 and "
                            "17:00 (recurring daily time pattern).", "refs": [group[0]["ids"][0]]})

        incident_groups = defaultdict(list)
        for incident in dom_inc:
            incident_groups[(incident["site"], incident["category"])].append(incident)
        for (site, category), rows in sorted(incident_groups.items()):
            if len(rows) >= 2:
                months = sorted({parse_time(i["time"]).strftime("%Y-%m") for i in rows})
                dates = sorted({parse_time(i["time"]).date().isoformat() for i in rows})
                recurring = len(rows) >= 3 or len(months) >= 2
                out.append({"kind": "recurring_pattern" if recurring else "pattern",
                            "dimensions": {"site": site, "incident_category": category, "months": months,
                                           "dates": dates},
                            "statement": ("Recurring Pattern Detected: " if recurring else "")
                            + f"{len(rows)} {category} incidents at {self.site_label(site)}"
                            + (f" across {', '.join(months)}" if len(months) >= 2 else
                               f" on {len(dates)} distinct date(s) in {months[0]}")
                            + "; recurrence alone does not establish a cause.",
                            "refs": [i["id"] for i in rows]})

        by_category = defaultdict(list)
        for incident in dom_inc:
            by_category[incident["category"]].append(incident)
        for category, rows in sorted(by_category.items()):
            sites = sorted({i["site"] for i in rows})
            if len(sites) >= 2 and len(rows) >= 3:
                out.append({"kind": "cross_site_pattern", "dimensions": {"sites": sites,
                            "incident_category": category}, "statement": f"{len(rows)} {category} incidents recur "
                            f"across {len(sites)} sites; this cross-site correlation is not a proven shared cause.",
                            "refs": [i["id"] for i in rows]})

        assets = defaultdict(list)
        for incident in dom_inc:
            for asset in re.findall(r"\b([A-Z]{2,8}-[A-Z0-9]+)\b", incident["summary"]):
                assets[asset].append(incident)
        for asset, rows in sorted(assets.items()):
            if len(rows) >= 2:
                out.append({"kind": "equipment_recurrence", "dimensions": {"equipment": asset},
                            "statement": f"Recurring Pattern Detected: {asset} appears in {len(rows)} incidents; "
                            "diagnostic evidence is still required before attributing causation.",
                            "refs": [i["id"] for i in rows]})

        maintenance_groups = defaultdict(list)
        for change in self.maintenance:
            maintenance_groups[(change["site"], change["asset"])].append(change)
        for (site, asset), rows in sorted(maintenance_groups.items()):
            if len(rows) >= 2:
                out.append({"kind": "maintenance_recurrence", "dimensions": {"site": site, "equipment": asset},
                            "statement": f"Recurring Pattern Detected: {len(rows)} maintenance events involve "
                            f"{asset} at {self.site_label(site)}; temporal recurrence is not a root-cause finding.",
                            "refs": [m["id"] for m in rows]})

        if hot_inc:
            out.append({"kind": "correlation", "dimensions": {"window_minutes": 90},
                        "statement": f"{len(hot_inc)} of {len(self.incidents)} incidents occurred within 90 min "
                        f"of an inlet reading above {THERMAL_THRESHOLD_C:g} °C; this is correlation, not causation.",
                        "refs": [i["id"] for i, _ in hot_inc]})
        unrelated = [i for i in self.incidents if i not in dom_inc and all(i is not h for h, _ in hot_inc)]
        if unrelated and self.domain != "general":
            out.append({"kind": "noise", "statement": f"{len(unrelated)} incident(s) show no observed link to "
                        "the problem and were excluded from hypothesis scoring.",
                        "refs": [i["id"] for i in unrelated]})
        return out

    # ------------------------------------------------------------ 3. hypotheses
    def _hypotheses(self, eps, dom_inc, hot_inc, p_start, p_end):
        hyps = []
        site = (eps[0]["site"] if eps else dom_inc[0]["site"]) if (eps or dom_inc) else None
        label = self.site_label(site) if site else "the site"
        sensor = eps[0]["sensor"] if eps else "no telemetry"
        cooling_mnt = [m for m in self.maintenance if asset_class(m["asset"]) == "cooling"
                       and (not site or m["site"] == site)]

        # A. cooling / thermal environment
        if eps or (self.domain == "thermal" and dom_inc):
            title = (f"Cooling capacity loss at {label} ({sensor})" if self.domain != "network"
                     else f"Thermal stress on network equipment at {label}")
            h = {"id": "H1", "kind": "cooling", "title": title, "supporting": [], "contradicting": [],
                 "missing": [], "chain": []}
            if len(eps) >= 2:
                h["supporting"].append(_ev(f"Inlet temperature exceeded {THERMAL_THRESHOLD_C:g} °C on {len(eps)} "
                                           f"separate periods (peak {max(e['peak'] for e in eps)} °C).", "moderate",
                                           [e["peak_id"] for e in eps]))
            elif eps:
                h["supporting"].append(_ev(f"Inlet temperature peaked at {eps[0]['peak']} °C.", "moderate",
                                           [eps[0]["peak_id"]]))
            if eps and eps[-1].get("recovery_id"):
                h["supporting"].append(_ev(f"Temperature returned below threshold by "
                                           f"{_fmt(eps[-1]['recovery_time'])} and stayed normal.", "moderate",
                                           [eps[-1]["recovery_id"]]))
                h["chain"].append({"step": "Temperature back to normal", "strength": "strong",
                                   "refs": [eps[-1]["recovery_id"]], "time": eps[-1]["recovery_time"]})
            thermal_alarms = [i for i in self.incidents if i["category"] == "thermal"]
            if thermal_alarms:
                h["supporting"].append(_ev(f"{len(thermal_alarms)} thermal alarm incident(s) raised.", "moderate",
                                           [i["id"] for i in thermal_alarms]))
                h["chain"].append({"step": "Thermal alarm raised", "strength": "moderate",
                                   "refs": [i["id"] for i in thermal_alarms],
                                   "time": parse_time(thermal_alarms[0]["time"])})
            if eps:
                h["chain"].append({"step": f"Inlet temperature above {THERMAL_THRESHOLD_C:g} °C", "strength": "strong",
                                   "refs": [eps[0]["ids"][0]], "time": eps[0]["start"]})
            net_hot = [i for i, _ in hot_inc if i["category"] == "network"]
            if net_hot:
                strength = "strong" if self.domain == "network" and len(net_hot) >= 0.6 * max(1, len(dom_inc)) \
                    else "moderate"
                h["supporting"].append(_ev(f"{len(net_hot)} network incidents occurred during high inlet "
                                           "temperature.", strength, [i["id"] for i in net_hot]))
                h["chain"].append({"step": "Network equipment incidents", "strength": "moderate",
                                   "refs": [i["id"] for i in net_hot], "time": parse_time(net_hot[0]["time"])})
            if "maintenance" in self.have:
                for m in cooling_mnt:
                    m_start, m_end = parse_time(m["start"]), parse_time(m["end"])
                    if eps and m_start <= eps[0]["start"] and m_end >= eps[-1]["end"] - timedelta(hours=1):
                        h["supporting"].append(_ev(f"{m['asset']} was offline ({m['id']}) from {_fmt(m_start)} to "
                                                   f"{_fmt(m_end)}, covering every exceedance period.", "strong",
                                                   [m["id"]]))
                        h["chain"].append({"step": f"{m['asset']} taken offline", "strength": "strong",
                                           "refs": [m["id"]], "time": m_start})
                        rec = eps[-1].get("recovery_time")
                        if rec and timedelta(0) <= rec - m_end <= timedelta(hours=12):
                            h["supporting"].append(_ev(f"Temperature normalized within "
                                                       f"{int((rec - m_end).total_seconds() // 3600)} h of "
                                                       f"{m['asset']} returning to service.", "strong",
                                                       [m["id"], eps[-1]["recovery_id"]]))
                            h["chain"].append({"step": f"{m['asset']} restored", "strength": "strong",
                                               "refs": [m["id"]], "time": m_end})
                if not cooling_mnt:
                    h["missing"].append({"what": "No cooling maintenance recorded; cooling-unit fault logs needed",
                                         "tool": None})
            else:
                h["missing"].append({"what": "Cooling equipment maintenance / state records", "tool": "maintenance",
                                     "critical": "maintenance" not in self.attempted})
            if "telemetry" not in self.have:
                h["missing"].append({"what": "Inlet temperature telemetry", "tool": "telemetry",
                                     "critical": "telemetry" not in self.attempted})
            h["missing"].append({"what": "HVAC/CRAC controller telemetry (not connected to Ops data)", "tool": None})
            hyps.append(h)

        # B. external heat
        if self.weather and (eps or self.domain == "thermal"):
            anomaly_days = {t.date().isoformat() for e in eps for t in e["times"]}
            hot = [w for w in self.weather if w["date"] in anomaly_days]
            calm = [w for w in self.weather if w["date"] not in anomaly_days]
            h = {"id": "H2", "kind": "weather", "title": f"External heatwave overloading cooling at {label}",
                 "supporting": [], "contradicting": [], "missing": [], "chain": []}
            if hot:
                avg_hot = sum(w["tmax"] for w in hot) / len(hot)
                if avg_hot >= 40:
                    h["supporting"].append(_ev(f"Outdoor maximum averaged {avg_hot:.1f} °C on anomaly days.",
                                               "moderate", [w["id"] for w in hot]))
                similar = [w for w in calm if abs(w["tmax"] - avg_hot) <= 1.5]
                if similar:
                    h["contradicting"].append(_ev(f"Similar outdoor heat on {', '.join(w['date'][5:] for w in similar)}"
                                                  " with normal inlet temperature.", "strong",
                                                  [w["id"] for w in similar]))
            hyps.append(h)

        # C. recurring power quality / UPS alternatives
        if self.domain == "power" and dom_inc:
            power_assets = Counter(a for i in dom_inc for a in re.findall(r"\b(?:UPS|PDU|ATS)-[A-Z0-9]+\b",
                                                                          i["summary"].upper()))
            power_asset = power_assets.most_common(1)[0][0] if power_assets else "site power path"
            utility_rows = [i for i in dom_inc if any(term in i["summary"].lower() for term in
                                                       ("utility", "grid", "voltage sag", "undervoltage"))]
            power_maintenance = [m for m in self.maintenance if m["site"] == site and
                                 (asset_class(m["asset"]) == "power" or
                                  any(m["asset"].upper().startswith(x) for x in ("UPS", "PDU", "ATS")))]
            upstream = {"id": "", "kind": "power_quality",
                        "title": f"Recurring upstream voltage instability affecting {power_asset} at {label}",
                        "supporting": [], "contradicting": [], "missing": [], "chain": []}
            if len(dom_inc) >= 3:
                upstream["supporting"].append(_ev(
                    f"{len(dom_inc)} power incidents recurred across the requested window.", "strong",
                    [i["id"] for i in dom_inc]))
            if utility_rows:
                upstream["supporting"].append(_ev(
                    f"{len(utility_rows)} incident(s) explicitly record utility input sag or undervoltage before "
                    f"{power_asset} transfer warnings.", "strong", [i["id"] for i in utility_rows]))
            if power_assets:
                upstream["supporting"].append(_ev(
                    f"{power_asset} appears in {power_assets[power_asset]} recurring power incidents.", "moderate",
                    [i["id"] for i in dom_inc if power_asset in i["summary"].upper()]))
            upstream["chain"] = [{"step": i["summary"], "strength": "moderate", "refs": [i["id"]],
                                  "time": parse_time(i["time"])} for i in dom_inc]
            if "maintenance" not in self.have:
                upstream["missing"].append({"what": f"{power_asset} diagnostic and maintenance records",
                                            "tool": "maintenance", "critical": "maintenance" not in self.attempted})
            elif power_maintenance:
                diagnostic = [m for m in power_maintenance if any(term in m["description"].lower() for term in
                                                                   ("self-test passed", "input logs", "undervoltage"))]
                if diagnostic:
                    upstream["supporting"].append(_ev(
                        f"{diagnostic[0]['asset']} diagnostics passed while input logs recorded utility "
                        "undervoltage; this supports an upstream-feed hypothesis rather than a confirmed UPS defect.",
                        "strong", [diagnostic[0]["id"]]))
                    upstream["chain"].append({"step": f"{diagnostic[0]['asset']} diagnostic inspection",
                                              "strength": "strong", "refs": [diagnostic[0]["id"]],
                                              "time": parse_time(diagnostic[0]["start"])})
                else:
                    upstream["missing"].append({"what": f"Direct {power_asset} self-test and input diagnostics",
                                                "tool": None})
            else:
                upstream["missing"].append({"what": f"No {power_asset} maintenance record found", "tool": None})
            upstream["missing"].append({"what": "Power-quality telemetry (voltage and frequency) is not connected",
                                        "tool": None})
            hyps.append(upstream)

            ups_fault = {"id": "", "kind": "ups_fault", "title": f"Internal fault in {power_asset}",
                         "supporting": [], "contradicting": [], "missing": [], "chain": []}
            ups_refs = [i["id"] for i in dom_inc if power_asset in i["summary"].upper()]
            if ups_refs:
                ups_fault["supporting"].append(_ev(
                    f"{power_asset} produced transfer or battery warnings in {len(ups_refs)} incident(s).",
                    "moderate", ups_refs))
            if utility_rows:
                ups_fault["contradicting"].append(_ev(
                    "The incident records identify upstream utility undervoltage before the UPS warnings.",
                    "strong", [i["id"] for i in utility_rows]))
            passed = [m for m in power_maintenance if "self-test passed" in m["description"].lower()]
            if passed:
                ups_fault["contradicting"].append(_ev(
                    f"{passed[0]['asset']} self-test passed after the incidents.", "strong", [passed[0]["id"]]))
            else:
                ups_fault["missing"].append({"what": f"Direct {power_asset} self-test / diagnostic logs",
                                             "tool": "maintenance" if "maintenance" not in self.attempted else None,
                                             "critical": "maintenance" not in self.attempted})
            hyps.append(ups_fault)

        # D. equipment-side cause
        assets = Counter(a for i in dom_inc for a in re.findall(r"\b([A-Z]{2,5}-[A-Z0-9]+)\b", i["summary"]))
        if self.domain == "network" and assets:
            asset, n = assets.most_common(1)[0]
            h = {"id": "H3", "kind": "equipment", "title": f"Localized hardware fault on {asset}",
                 "supporting": [], "contradicting": [], "chain": [],
                 "missing": [{"what": f"Device syslog / hardware diagnostics for {asset}", "tool": None}]}
            refs = [i["id"] for i in dom_inc if asset in i["summary"]]
            if n >= 2:
                h["supporting"].append(_ev(f"{n} incidents reference {asset}.", "moderate", refs))
            if hot_inc and len([i for i, _ in hot_inc if i["category"] == "network"]) >= 0.6 * len(dom_inc):
                h["contradicting"].append(_ev("Incidents cluster only in high-temperature periods, which points "
                                              "to an environmental trigger.", "moderate", refs))
            hyps.append(h)
        elif self.domain == "thermal" and eps:
            h = {"id": "H3", "kind": "equipment", "title": f"Server-side / IT load increase in {sensor}",
                 "supporting": [], "contradicting": [], "chain": [],
                 "missing": [{"what": "Rack power / IT load telemetry", "tool": None}]}
            related = [i for i in self.incidents if i["category"] in ("thermal", "network", "power")]
            later = [i for i in related if parse_time(i["time"]) > eps[0]["start"]]
            if later and len(later) == len(related):
                h["contradicting"].append(_ev("Temperature rise began before every related incident, so "
                                              "equipment warnings follow the environment, not lead it.", "moderate",
                                              [eps[0]["ids"][0], later[0]["id"]]))
            hyps.append(h)

        # D. changes / maintenance (non-cooling, or any change in change/general domain)
        for m in self.maintenance:
            if site and m["site"] != site:
                continue
            if asset_class(m["asset"]) == "cooling" and any(x["kind"] == "cooling" for x in hyps):
                continue
            if not p_start:
                continue
            m_start, m_end = parse_time(m["start"]), parse_time(m["end"])
            if not (p_start - timedelta(hours=72) <= m_start <= p_end):
                continue
            h = {"id": f"H{len(hyps) + 1}", "kind": "change", "title": f"Change {m['id']} on {m['asset']} "
                 f"({m['kind'].replace('_', ' ')})", "supporting": [], "contradicting": [], "missing": [], "chain": []}
            gap_h = (p_start - m_end).total_seconds() / 3600
            if gap_h >= 0:
                h["supporting"].append(_ev(f"Completed {gap_h:.0f} h before the problem started.", "moderate",
                                           [m["id"]]))
            cls = asset_class(m["asset"])
            if self.domain in ("thermal", "network", "power") and cls not in (self.domain, "other") \
                    and not (self.domain == "thermal" and cls == "cooling"):
                h["contradicting"].append(_ev(f"{m['asset']} is a {cls} asset; no direct mechanism for a "
                                              f"{self.domain} problem.", "moderate", [m["id"]]))
            window_end = parse_time(self.plan["window"]["end"] + "T23:59:00+05:30")
            if m["kind"] in PERSISTENT_CHANGES and window_end - p_end > timedelta(hours=12):
                h["contradicting"].append(_ev(f"Problem stopped after {_fmt(p_end)} while the change is still in "
                                              "place.", "strong", [m["id"]]))
            if m["kind"] not in PERSISTENT_CHANGES and m_start <= p_start and m_end >= p_end:
                h["supporting"].append(_ev("Problem occurred only while this work was in progress.", "strong",
                                           [m["id"]]))
            hyps.append(h)

        if not hyps and dom_inc:
            h = {"id": "H1", "kind": "isolated", "title": "Isolated event - no correlated cause in collected data",
                 "supporting": [], "contradicting": [], "chain": [],
                 "missing": [{"what": "Device / facility logs for the affected equipment", "tool": None}]}
            if len(dom_inc) == 1:
                h["supporting"].append(_ev("Single incident with no recurrence in the window.", "moderate",
                                           [dom_inc[0]["id"]]))
            hyps.append(h)

        for h in hyps:
            h["confidence"] = _score(h)
            h["label"] = _label(h["confidence"])
        hyps.sort(key=lambda h: -h["confidence"])
        for rank, h in enumerate(hyps, 1):
            h["rank"], h["id"] = rank, f"H{rank}"  # unique IDs in ranked order
        return hyps

    # ------------------------------------------------------------ 4. problem / impact / risks / unknowns
    def _problem(self, eps, dom_inc, p_start, p_end):
        if self.domain == "thermal" and eps:
            s = eps[0]["site"]
            return {"title": f"Cooling anomaly at {self.site_label(s)}",
                    "statement": f"{eps[0]['sensor']} inlet temperature at {self.site_label(s)} exceeded "
                                 f"{THERMAL_THRESHOLD_C:g} °C in {len(eps)} periods from {_fmt(p_start)} to "
                                 f"{_fmt(p_end)} (peak {max(e['peak'] for e in eps)} °C).",
                    "refs": [e["peak_id"] for e in eps]}
        if dom_inc:
            s = Counter(i["site"] for i in dom_inc).most_common(1)[0][0]
            kind = {"network": "Repeated network incidents", "power": "Power events",
                    "thermal": "Thermal alarms"}.get(self.domain, "Incident cluster")
            return {"title": f"{kind} at {self.site_label(s)}",
                    "statement": (f"1 {dom_inc[0]['severity']} incident at {_fmt(p_start)}: {dom_inc[0]['summary']}."
                                  if len(dom_inc) == 1 else
                                  f"{len(dom_inc)} incidents between {_fmt(p_start)} and {_fmt(p_end)}."),
                    "refs": [i["id"] for i in dom_inc]}
        return {"title": "No anomaly found in available data",
                "statement": "The collected data shows no threshold breach or matching incident cluster "
                             "for this request.", "refs": []}

    def _impact(self, period_inc, eps):
        sev = Counter(i["severity"] for i in period_inc)
        assets = sorted({a for i in period_inc for a in re.findall(r"\b([A-Z]{2,5}-[A-Z0-9]+)\b", i["summary"])})
        p1 = [i for i in period_inc if i["severity"] == "P1"]
        text = (f"{len(period_inc)} incident{'s' if len(period_inc) != 1 else ''} during the problem period "
                f"({', '.join(f'{k}: {v}' for k, v in sorted(sev.items()))}). " if period_inc else
                "No incidents recorded during the problem period. ")
        if p1:
            text += f"{len(p1)} P1 service-affecting event{'s' if len(p1) != 1 else ''}, e.g. \"{p1[0]['summary']}\". "
        if assets:
            text += f"Affected assets: {', '.join(assets)}."
        return {"statement": text.strip(), "p1": len(p1), "incidents": len(period_inc), "assets": assets,
                "refs": [i["id"] for i in p1] or [i["id"] for i in period_inc][:3]}

    def _risks(self, eps, period_inc, hyps):
        risks = []
        if eps:
            risks.append({"risk": "Hardware running above the ASHRAE recommended inlet range wears faster and can "
                          "trigger thermal shutdowns.", "refs": [max(eps, key=lambda e: e["peak"])["peak_id"]]})
        if any(h["kind"] == "cooling" and h["confidence"] >= 60 for h in hyps):
            risks.append({"risk": "The next planned cooling maintenance could repeat the outage unless "
                          "redundancy is verified first.", "refs": [r for e in hyps[0]["supporting"]
                                                                     for r in e["refs"]][:2]})
        if any(i["severity"] == "P1" for i in period_inc):
            risks.append({"risk": "Customer-facing services were interrupted; repeat events risk SLA penalties.",
                          "refs": [i["id"] for i in period_inc if i["severity"] == "P1"]})
        return risks

    def _unknowns(self, hyps):
        out = []
        for tool, c in self.collected.items():
            if c["status"] == "degraded_fallback":
                out.append({"what": f"{tool}: primary source failed ({len(c['errors'])} errors); data served from "
                            f"{c['source']}.", "tool": None, "kind": "degraded"})
            elif c["status"] == "failed":
                out.append({"what": f"{tool}: primary and fallback sources failed - no data, analysis limited.",
                            "tool": tool, "kind": "gap"})
            for gap in c.get("missing_fields", []):
                # A failed empty source is already represented by the source-level gap above.
                if c["status"] == "failed" and gap.get("record_id") is None:
                    continue
                record = gap.get("record_id") or "empty result"
                out.append({"what": f"{tool}: {record} missing normalized field(s): "
                                    f"{', '.join(gap['fields'])}.",
                            "tool": None, "kind": "field_gap", "record_id": gap.get("record_id"),
                            "fields": gap["fields"]})
        seen = set()
        for h in hyps[:2]:
            for m in h["missing"]:
                if m["what"] not in seen:
                    seen.add(m["what"])
                    out.append({"what": m["what"], "tool": m.get("tool"), "kind": "missing_evidence"})
        return out

    # ------------------------------------------------------------ 5. Action & Opportunity Engine
    def _actions(self, hyps, eps, hot_inc, unknowns, period_inc, p_end):
        actions = []
        top = hyps[0] if hyps else None
        p1 = any(i["severity"] == "P1" for i in period_inc)
        window_end = parse_time(self.plan["window"]["end"] + "T23:59:00+05:30")
        ongoing = bool(p_end and window_end - p_end < timedelta(hours=6))
        top_refs = [r for e in (top["supporting"] if top else []) for r in e["refs"]]

        if top and top["confidence"] >= ACTION_CONFIDENCE:
            prio = "Critical" if (ongoing and p1 and top["confidence"] >= 70) else \
                "High" if (p1 or top["confidence"] >= 70) else "Medium"
            if top["kind"] == "cooling":
                asset = next((m["asset"] for m in self.maintenance if asset_class(m["asset"]) == "cooling"),
                             "cooling units")
                actions.append({
                    "type": "maintenance_ticket", "issue": top["title"], "priority": prio,
                    "recommended": f"Create maintenance ticket: inspect {asset} and HVAC controller logs, and "
                                   "verify N+1 cooling redundancy before any further cooling maintenance.",
                    "owner": "Facilities", "effort": "Low", "requires_approval": True, "hypothesis": top["id"],
                    "confidence": top["confidence"], "evidence": top_refs,
                    "reason": top["supporting"][0]["statement"] if top["supporting"] else top["title"],
                    "opportunity": "Recurring thermal exceedances during planned cooling work indicate an "
                                   "opportunity for an automated maintenance-risk check that alerts when cooling "
                                   "redundancy drops."})
            elif top["kind"] == "power_quality":
                asset = next((m["asset"] for m in self.maintenance if asset_class(m["asset"]) == "power"),
                             "utility feed and UPS input path")
                actions.append({
                    "type": "maintenance_ticket", "issue": top["title"], "priority": prio,
                    "recommended": f"Create maintenance ticket: inspect the utility feed and ATS serving {asset}, "
                                   "preserve UPS input logs, and verify power quality before replacing UPS hardware.",
                    "owner": "Facilities", "effort": "Medium", "requires_approval": True,
                    "hypothesis": top["id"], "confidence": top["confidence"], "evidence": top_refs,
                    "reason": " ".join(e["statement"] for e in top["supporting"][:4]) if top["supporting"] else top["title"],
                    "opportunity": "Recurring utility-input and UPS-transfer sequences indicate an opportunity "
                                   "for automated power-quality correlation and early warning."})
            elif top["kind"] == "change":
                actions.append({
                    "type": "change_review", "issue": top["title"], "priority": prio,
                    "recommended": "Open a change review for this change and prepare a tested rollback plan.",
                    "owner": "Infrastructure", "effort": "Medium", "requires_approval": True,
                    "hypothesis": top["id"], "confidence": top["confidence"], "evidence": top_refs,
                    "reason": top["supporting"][0]["statement"] if top["supporting"] else top["title"],
                    "opportunity": "Change-correlated incidents indicate an opportunity for automated change-risk "
                                   "scoring before approval."})
            else:
                actions.append({
                    "type": "maintenance_ticket", "issue": top["title"], "priority": prio,
                    "recommended": "Create a ticket to inspect the affected equipment and collect diagnostics.",
                    "owner": "Infrastructure", "effort": "Medium", "requires_approval": True,
                    "hypothesis": top["id"], "confidence": top["confidence"], "evidence": top_refs,
                    "reason": top["title"], "opportunity": "Repeated incidents indicate an opportunity for "
                                                          "automated anomaly detection."})
        elif top:
            need = sorted({u["tool"] for u in unknowns if u.get("tool")})
            actions.append({
                "type": "evidence_request", "issue": f"Weak evidence: {top['title']}", "priority": "Medium",
                "recommended": "Collect more evidence before acting: "
                               + (f"restore and re-collect {', '.join(need)} data, then re-run the investigation"
                                  if need else "manual inspection of the affected equipment") + ".",
                "owner": "IT Operations", "effort": "Low", "requires_approval": False, "hypothesis": top["id"],
                "confidence": top["confidence"], "evidence": top_refs, "reason": f"Top hypothesis confidence "
                                                                                 f"is only {top['confidence']}%.",
                "opportunity": None})

        net_hot = [i for i, _ in hot_inc if i["category"] == "network"]
        if net_hot:
            assets = Counter(a for i in net_hot for a in re.findall(r"\b([A-Z]{2,5}-[A-Z0-9]+)\b", i["summary"]))
            asset = assets.most_common(1)[0][0] if assets else "network equipment"
            actions.append({
                "type": "investigation_task", "issue": f"Network incidents during thermal exceedance ({asset})",
                "priority": "High" if any(i["severity"] == "P1" for i in net_hot) else "Medium",
                "recommended": f"Review {asset} environmental sensors and hardware health logs for the exceedance "
                               "periods; confirm no lasting damage.",
                "owner": "Infrastructure", "effort": "Low", "requires_approval": False,
                "hypothesis": top["id"] if top else None, "confidence": top["confidence"] if top else 0,
                "evidence": [i["id"] for i in net_hot], "reason": f"{len(net_hot)} network incidents coincided "
                                                                   "with high inlet temperature.",
                "opportunity": "Correlating facility telemetry with network incidents automatically enables "
                               "cross-domain anomaly detection."})

        for u in unknowns:
            if u["kind"] == "missing_evidence" and not u.get("tool") and "not connected" in u["what"]:
                actions.append({
                    "type": "integration", "issue": "Missing data: " + u["what"], "priority": "Low",
                    "recommended": "Connect this data source to the Ops data pipeline so future investigations "
                                   "can use it.", "owner": "IT Operations", "effort": "Medium",
                    "requires_approval": False, "hypothesis": None, "confidence": None, "evidence": [],
                    "reason": "Evidence needed to confirm the leading hypothesis is not collected today.",
                    "opportunity": "Sensor coverage gaps indicate an opportunity for a telemetry integration "
                                   "service."})
            if u["kind"] in ("degraded", "gap"):
                actions.append({
                    "type": "reliability", "issue": "Data source reliability: " + u["what"].split(":")[0],
                    "priority": "Medium" if u["kind"] == "gap" else "Low",
                    "recommended": "Investigate the failing primary data source and add health monitoring.",
                    "owner": "IT Operations", "effort": "Low", "requires_approval": False, "hypothesis": None,
                    "confidence": None, "evidence": [], "reason": u["what"],
                    "opportunity": "Tool failures during investigations indicate an opportunity for data-pipeline "
                                   "health monitoring."})
        actions.sort(key=lambda a: PRIORITY_ORDER[a["priority"]])
        return actions

    # ------------------------------------------------------------ timeline
    def _timeline(self, eps, period_inc, p_start, p_end):
        if not p_start:
            return []
        lo, hi = p_start - timedelta(hours=14), p_end + timedelta(hours=14)
        items = []
        for m in self.maintenance:
            for when, what, kind in ((m["start"], f"{m['asset']} maintenance started: {m['description']}", "warning"),
                                     (m["end"], f"{m['asset']} maintenance ended", "recovery")):
                t = parse_time(when)
                if lo <= t <= hi:
                    items.append((t, what, kind, [m["id"]]))
        for e in eps:
            items.append((e["start"], f"{e['sensor']} inlet above {THERMAL_THRESHOLD_C:g} °C", "alert", [e["ids"][0]]))
            if e.get("recovery_time"):
                items.append((e["recovery_time"], f"{e['sensor']} inlet back to normal", "normal",
                              [e["recovery_id"]]))
        for i in period_inc:
            items.append((parse_time(i["time"]), f"{i['severity']} {i['category']}: {i['summary']}",
                          "alert" if i["severity"] in ("P1", "P2") else "info", [i["id"]]))
        items.sort(key=lambda x: x[0])
        return [{"time": _fmt(t), "iso": t.isoformat(), "label": label, "kind": kind, "refs": refs}
                for t, label, kind, refs in items]


# ---------------------------------------------------------------- agent entry points
def analyze(plan, collected):
    """Returns (result, set_of_valid_evidence_ids)."""
    a = Analysis(plan, collected)
    return a.run(), a.evidence_ids


def write_summary(result, feedback=None, faults=None):
    """Executive summary (LLM-worded if configured). Returns (text, meta)."""
    top = result["hypotheses"][0] if result["hypotheses"] else None
    gaps = [u for u in result["unknowns"] if u["kind"] in ("degraded", "gap")]
    refs = " ".join(f"[{r}]" for r in result["problem"]["refs"][:2])
    fallback = f"{result['problem']['statement']} {refs}".strip()
    if top:
        top_refs = " ".join(f"[{r}]" for e in top["supporting"][:2] for r in e["refs"][:1])
        fallback += (f" Leading hypothesis: {top['title']} ({top['label'].lower()} confidence, {top['confidence']}%)"
                     f" {top_refs}.")
        if len(result["hypotheses"]) > 1:
            alt = result["hypotheses"][1]
            fallback += f" Alternative considered: {alt['title']} ({alt['confidence']}%)."
    if result["actions"]:
        fallback += f" Recommended next action: {result['actions'][0]['recommended']}"
    if gaps:
        fallback += " Data limitations: " + "; ".join(g["what"] for g in gaps)
    if result.get("input_note"):
        fallback += " " + result["input_note"]
    prompt = (f"Facts (JSON): {dict(problem=result['problem'], impact=result['impact'], hypotheses=[{k: h[k] for k in ('title', 'confidence', 'supporting', 'contradicting')} for h in result['hypotheses'][:3]], unknowns=result['unknowns'], next_action=result['actions'][0]['recommended'] if result['actions'] else None)}\n\n"
              "Write a 3-5 sentence executive summary. Cite evidence IDs in [brackets] exactly as given. "
              "State causes as hypotheses with confidence. Mention any data limitations.")
    if feedback:
        prompt += f"\nA reviewer rejected the previous draft: {feedback}. Fix these issues."
    text, meta = call_llm(SYSTEM, prompt, fallback=fallback, max_tokens=350)
    fault = faults.next("intelligence") if faults else None
    if fault == "hallucinate":  # simulated LLM failure: invented evidence ID
        text += " This is the confirmed root cause [INC-9999]."
        meta["fault_injected"] = "hallucinated citation"
    elif fault == "overclaim":  # simulated LLM failure: correlation stated as proven causation
        text += f" The {result.get('focus_component') or 'equipment'} is definitely defective and is the confirmed root cause."
        meta["fault_injected"] = "overclaimed causation"
    return text.strip(), meta


# ---------------------------------------------------------------- individual mode: operator-provided records
LINE = re.compile(r"^\s*\[?(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?(?P<time>\d{1,2}:\d{2})(?::\d{2})?\]?\s*"
                  r"[-–—|:,]?\s*(?P<text>\S.*?)\s*$")
EPISODE_GAP_MIN = 20  # records further apart than this start a new episode
# (keyword regex, component key, display name, hypothesis title, evidence needed to confirm, owner)
COMPONENTS = [
    # Word-anchored: 'follow-ups', 'attempt', 'template', 'school', 'theater' must not match; plurals must.
    (r"(?<![\w-])ups(?!\w)", "ups", "UPS", "UPS-related fault", ["UPS diagnostic logs",
                                                                      "Input voltage measurements"],
     "Facilities / Electrical"),
    (r"\bpower|\bvoltage|\bgrid\b|\bfluctuat|\bbrownout|\bsurge|\bsags?\b|\bmains\b", "power", "power supply",
     "Upstream power-quality issue", ["Power-quality telemetry (voltage, frequency)", "Utility / grid event records"],
     "Facilities / Electrical"),
    (r"\btemp(?:erature)?s?\b|\bthermal|\bcool(?:ing|er|ant)?\b|\bhvac|\bcrac|\bheat|\bchiller|\boverheat",
     "cooling", "cooling", "Cooling / HVAC issue",
     ["HVAC controller telemetry", "Rack inlet temperature telemetry"], "Facilities"),
    (r"\bnetwork|\bswitch(?:es)?\b|\blinks?\b|\bpackets?\b|\blatency|\brouters?\b|\bvlans?\b|\bports?\b",
     "network", "network equipment",
     "Network equipment issue", ["Device syslog", "Interface error counters"], "Infrastructure"),
    (r"\bdisks?\b|\bstorage|\braid\b|\bsan\b|\bnas\b", "storage", "storage", "Storage issue",
     ["Storage controller logs"], "IT Operations"),
    (r"\bservers?\b|\bhosts?\b|\bnodes?\b|\bvms?\b|\bbmc\b|\bcpus?\b", "server", "servers", "Server-side fault",
     ["Server hardware / BMC logs", "Server power and temperature telemetry"], "IT Operations"),
]
ALERT_WORDS = ("warning", "alarm", "alert", "fail", "error", "critical", "down", "trip")


class InputError(ValueError):
    """Operator input could not be turned into records (reported as Validation Failed)."""


def _component(text):
    t = text.lower()
    for rx, key, name, title, needed, owner in COMPONENTS:
        if re.search(rx, t):
            return {"key": key, "name": name, "title": title, "needed": needed, "owner": owner}
    return {"key": "other", "name": "equipment", "title": "Equipment issue", "needed": ["Equipment logs"],
            "owner": "IT Operations"}


def parse_records(text):
    """'HH:MM description' lines (optional YYYY-MM-DD) or a JSON list -> ordered events. Raises InputError."""
    import json as _json
    from datetime import datetime

    events, skipped = [], 0
    items = text if isinstance(text, list) else None  # already-decoded JSON list (API / Python callers)
    raw = "" if items is not None else str(text or "").strip()
    if items is None and raw.startswith("[") and not LINE.match(raw.splitlines()[0]):  # '[10:05] x' is text
        try:
            items = _json.loads(raw)
        except ValueError as e:
            raise InputError(f"JSON input could not be parsed: {e}")
        if not isinstance(items, list):
            raise InputError("JSON input must be a list of records")
    if items is not None:
        lines = [_json_line(i) for i in items]
    else:
        lines = raw.splitlines()
    day, last = datetime(2000, 1, 1), None
    for line in lines:
        if not line or not line.strip():
            continue
        m = LINE.match(line)
        if not m:
            skipped += 1
            continue
        hh, mm = map(int, m.group("time").split(":"))
        if hh > 23 or mm > 59:  # '10:99' / '24:30' are invalid, not wrapped into another time
            skipped += 1
            continue
        if m.group("date"):
            try:
                day = datetime.strptime(m.group("date"), "%Y-%m-%d")
            except ValueError:
                raise InputError(f"invalid date '{m.group('date')}' in record: {line.strip()[:80]}")
            dt = day.replace(hour=hh, minute=mm)
        else:
            dt = day.replace(hour=hh, minute=mm)
            # Undated lines: only a large backwards jump (e.g. 23:50 -> 00:10) means the next day.
            # Small backwards steps are unsorted input and are ordered by the sort below.
            if last and last - dt > timedelta(hours=12):
                day = day + timedelta(days=1)
                dt = day.replace(hour=hh, minute=mm)
        last = dt
        label = re.sub(r"\s+", " ", m.group("text")).strip().rstrip(".")
        events.append({"dt": dt, "label": label, "key": label.lower(), "comp": _component(label),
                       "has_date": bool(m.group("date"))})
    if len(events) < 2:
        raise InputError("Could not find at least 2 timestamped records. Use one record per line, e.g. "
                         "'10:05 Power fluctuation'.")
    events.sort(key=lambda e: e["dt"])
    for n, e in enumerate(events, 1):
        e["id"] = f"EV-{n:02d}"
    return events, skipped


def _json_line(item):
    """One JSON record -> 'YYYY-MM-DD HH:MM text'. ISO times with seconds, 'Z' or an offset keep their
    local wall-clock time; the offset is not glued into the label (which broke recurrence matching)."""
    from datetime import datetime

    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    when = item.get("time") or item.get("opened") or item.get("timestamp") or ""
    text = item.get("summary") or item.get("label") or item.get("event") or item.get("message") or ""
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        when = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    else:
        when = str(when).strip()
        if re.match(r"\d{4}-\d{2}-\d{2}T", when):
            try:
                when = datetime.fromisoformat(when.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
    return f"{when} {text}".strip()


def _t(e):
    return e["dt"].strftime("%Y-%m-%d %H:%M") if e["has_date"] else e["dt"].strftime("%H:%M")


def analyze_records(text, objective=""):
    """Deterministic analysis of operator-provided records. Returns (result, evidence_ids, collected_status)."""
    events, skipped = parse_records(text)
    episodes, cur = [], [events[0]]
    for prev, e in zip(events, events[1:]):
        if (e["dt"] - prev["dt"]).total_seconds() / 60 > EPISODE_GAP_MIN:
            episodes.append(cur)
            cur = []
        cur.append(e)
    episodes.append(cur)
    seqs = [tuple(dict.fromkeys(e["key"] for e in ep)) for ep in episodes]
    common, reps = Counter(seqs).most_common(1)[0]
    recurring = reps >= 2 and len(common) >= 2
    dominant = [ep for ep, s in zip(episodes, seqs) if s == common]
    label_of = {e["key"]: e["label"] for e in events}

    def lag(a, b):
        gaps = []
        for ep in dominant:
            ta = next(x["dt"] for x in ep if x["key"] == a)
            tb = next(x["dt"] for x in ep if x["key"] == b)
            gaps.append((tb - ta).total_seconds() / 60)
        return sum(gaps) / len(gaps)

    order = list(common)
    lags = [(a, b, lag(a, b)) for a, b in zip(order, order[1:])] if recurring else []
    patterns, hyps = [], []
    all_ids = [e["id"] for e in events]
    if recurring:
        chain = " → ".join(label_of[k] for k in order)
        patterns.append({"kind": "pattern", "statement": f"Recurring sequence {chain} occurred in {reps} of "
                         f"{len(episodes)} episodes.", "refs": [ep[0]["id"] for ep in dominant]})
        patterns.append({"kind": "correlation", "statement": "; ".join(
            f"{label_of[b]} followed {label_of[a]} after ~{g:.0f} min" for a, b, g in lags) + ".",
            "refs": [x["id"] for x in dominant[0]]})
        if len(dominant) >= 3:
            starts = [ep[0]["dt"] for ep in dominant]
            iv = [(b - a).total_seconds() / 60 for a, b in zip(starts, starts[1:])]
            mean = sum(iv) / len(iv)
            if max(iv) - min(iv) <= 0.25 * mean:
                patterns.append({"kind": "pattern", "statement": f"Episodes repeat roughly every {mean:.0f} min.",
                                 "refs": [ep[0]["id"] for ep in dominant]})
    counts = Counter(e["key"] for e in events)
    singles = [e for e in events if counts[e["key"]] == 1]
    if singles and len(counts) > 1:
        patterns.append({"kind": "anomaly", "statement": f"{len(singles)} event type(s) occurred only once: "
                         + ", ".join(e["label"] for e in singles[:4]) + ".", "refs": [e["id"] for e in singles]})
    odd = [ep for ep, s in zip(episodes, seqs) if s != common]
    if recurring and odd:
        patterns.append({"kind": "anomaly", "statement": f"{len(odd)} episode(s) deviate from the recurring "
                         "sequence.", "refs": [ep[0]["id"] for ep in odd]})

    # ---- competing hypotheses (one per component in the dominant sequence) ----
    seen, comps = set(), []
    for k in order:
        c = next(e["comp"] for e in events if e["key"] == k)
        if c["key"] not in seen:
            seen.add(c["key"])
            comps.append((k, c))
    first_ids = lambda key: [next(x["id"] for x in ep if x["key"] == key) for ep in dominant]  # noqa: E731
    for i, (k, c) in enumerate(comps):
        rest = [cc["name"] for _, cc in comps[i + 1:]]
        h = {"id": "", "kind": c["key"], "component": c["name"], "owner": c["owner"],
             "title": c["title"] + (f" propagating to {' and '.join(rest)}" if i == 0 and rest else ""),
             "supporting": [], "contradicting": [], "chain": [],
             "missing": [{"what": w, "tool": None} for w in c["needed"]]}
        if i == 0 and recurring:
            h["supporting"].append(_ev(f"{label_of[k]} is the first event in {reps} of {reps} matching episodes.",
                                       "strong", first_ids(k)))
            h["supporting"].append(_ev(f"The same sequence recurred {reps} times, suggesting a systematic rather "
                                       "than random trigger.", "moderate", [ep[0]["id"] for ep in dominant]))
            if lags:
                a, b, g = lags[0]
                h["supporting"].append(_ev(f"Every {label_of[a]} was followed by {label_of[b]} within ~{g:.0f} min.",
                                           "moderate", first_ids(b)))
        elif i > 0 and recurring:
            prev = comps[i - 1][0]
            h["supporting"].append(_ev(f"{label_of[k]} appears in every matching episode.", "moderate", first_ids(k)))
            last = i == len(comps) - 1
            h["contradicting"].append(_ev(f"{label_of[k]} always occurs after {label_of[prev]}, which fits a "
                                          "downstream reaction better than an origin.",
                                          "strong" if last else "moderate", first_ids(k)))
        elif not recurring:
            h["supporting"].append(_ev(f"{counts[k]} {label_of[k]} record(s).", "moderate",
                                       [e["id"] for e in events if e["key"] == k]))
        h["chain"] = [{"step": x["label"], "strength": "moderate", "refs": [x["id"]], "time": x["dt"]}
                      for x in (dominant[0] if recurring else events[:6])]
        hyps.append(h)
    if recurring:
        hyps.append({"id": "", "kind": "coincidence", "component": None, "owner": None,
                     "title": "Unrelated events with coincidental timing", "supporting": [], "chain": [],
                     "contradicting": [_ev(f"The identical sequence repeated {reps} times.", "strong",
                                           [ep[0]["id"] for ep in dominant])],
                     "missing": []})
    for h in hyps:
        h["confidence"] = _score(h)
        h["label"] = _label(h["confidence"])
    hyps.sort(key=lambda h: -h["confidence"])
    for rank, h in enumerate(hyps, 1):
        h["rank"], h["id"] = rank, f"H{rank}"
    top = hyps[0]
    alt = next((h for h in hyps[1:] if h.get("component")), None)

    # ---- problem, impact, risks, unknowns ----
    span = f"{_t(events[0])} and {_t(events[-1])}"
    if recurring:
        problem = {"title": f"Recurring {label_of[order[0]].lower()} sequence",
                   "statement": f"{reps} episodes of the sequence {' → '.join(label_of[k] for k in order)} "
                                f"between {span}.", "refs": [ep[0]["id"] for ep in dominant]}
    else:
        problem = {"title": "Operational event review", "statement": f"{len(events)} events between {span}; "
                   "no recurring sequence found.", "refs": all_ids[:3]}
    downstream = [c for _, c in comps[1:] if c["key"] in ("server", "network", "storage")]
    affected = [e for e in events if any(w in e["key"] for w in ALERT_WORDS)]
    impact = {"statement": (f"Downstream {', '.join(c['name'] for c in downstream)} warnings in {reps} episode(s) "
                            "indicate possible service impact." if downstream and recurring else
                            f"{len(affected)} warning/alert records; service impact cannot be confirmed from the "
                            "provided records."), "refs": [e["id"] for e in affected][:4],
              "incidents": len(affected), "p1": 0, "assets": []}
    risks = []
    if recurring:
        risks.append({"risk": "The pattern is likely to repeat; downstream equipment may trip or shut down if the "
                      "trigger persists.", "refs": [ep[0]["id"] for ep in dominant]})
    if downstream:
        risks.append({"risk": f"Repeated stress on {downstream[0]['name']} can shorten hardware life.",
                      "refs": first_ids(comps[-1][0]) if recurring else []})
    unknowns = [{"what": m["what"], "tool": None, "kind": "missing_evidence"}
                for h in hyps[:2] for m in h["missing"]]
    unknowns = list({u["what"]: u for u in unknowns}.values())
    unknowns.append({"what": "Records were provided by the operator and are not validated against a system of "
                     "record.", "tool": None, "kind": "input"})
    if skipped:
        unknowns.append({"what": f"{skipped} line(s) had no timestamp and were ignored.", "tool": None,
                         "kind": "input"})

    # ---- Action & Opportunity Engine ----
    actions = []
    needed = [m["what"] for m in top["missing"]] + ([m["what"] for m in alt["missing"]] if alt else [])
    names = " and ".join(dict.fromkeys(c["name"] for _, c in comps[:2]))
    if top["confidence"] >= ACTION_CONFIDENCE and top.get("component"):
        actions.append({"type": "maintenance_ticket", "issue": top["title"],
                        "priority": "High" if downstream else "Medium",
                        "recommended": f"Create maintenance ticket: inspect the {top['component']} and review "
                                       f"{', '.join(needed[:2])}.", "owner": top["owner"], "effort": "Medium",
                        "requires_approval": True, "hypothesis": top["id"], "confidence": top["confidence"],
                        "evidence": [r for e in top["supporting"] for r in e["refs"]],
                        "reason": top["supporting"][0]["statement"] if top["supporting"] else top["title"],
                        "opportunity": "Repeated incidents indicate an opportunity for automated anomaly detection."})
    elif top.get("component"):
        actions.append({"type": "diagnostic_work_order", "issue": f"Confirm cause: {top['title']}",
                        "priority": "High" if downstream and recurring else "Medium",
                        "recommended": f"Create diagnostic work order: collect {', '.join(dict.fromkeys(needed))} "
                                       f"for the {names} during the next occurrence.",
                        "owner": top["owner"], "effort": "Low", "requires_approval": True,
                        "hypothesis": top["id"], "confidence": top["confidence"],
                        "evidence": [r for e in top["supporting"] for r in e["refs"]],
                        "reason": f"Leading hypothesis is only {top['confidence']}% (heuristic evidence-weight "
                                  "score); diagnostics are needed before any corrective change.",
                        "opportunity": None})
    if recurring:
        actions.append({"type": "automation", "issue": "Automate detection of the recurring sequence",
                        "priority": "Medium",
                        "recommended": f"Add an alert rule that correlates {label_of[order[0]]} with downstream "
                                       f"{label_of[order[-1]]} within {int(max(g for *_, g in lags) + 5)} min.",
                        "owner": "IT Operations", "effort": "Medium", "requires_approval": False,
                        "hypothesis": top["id"], "confidence": top["confidence"],
                        "evidence": [ep[0]["id"] for ep in dominant], "reason": problem["statement"],
                        "opportunity": "Repeated identical event sequences indicate an opportunity for automated "
                                       "sequence detection and early warning."})
    actions.append({"type": "integration", "issue": "Missing data: " + (needed[0] if needed else "telemetry"),
                    "priority": "Low", "recommended": "Connect " + ", ".join(dict.fromkeys(needed[:3])) +
                    " to the Ops data pipeline so this pattern can be confirmed automatically.",
                    "owner": "IT Operations", "effort": "Medium", "requires_approval": False, "hypothesis": None,
                    "confidence": None, "evidence": [], "reason": "Evidence needed to confirm the leading "
                                                                    "hypothesis is not available.",
                    "opportunity": "Telemetry gaps indicate an opportunity for a monitoring integration service."})
    actions.sort(key=lambda a: PRIORITY_ORDER[a["priority"]])
    _explain_actions(actions, impact=impact, risks=risks, unknowns=unknowns)

    timeline = [{"time": _t(e), "iso": e["dt"].isoformat(), "label": e["label"],
                 "kind": "alert" if any(w in e["key"] for w in ALERT_WORDS) else "warning", "refs": [e["id"]]}
                for e in events]
    chain = sorted(top["chain"], key=lambda c: c["time"])
    facts = [
        {"kind": "problem", "statement": problem["statement"], "refs": problem["refs"]},
        {"kind": "impact", "statement": impact["statement"], "refs": impact["refs"]},
    ] + [{"kind": p["kind"], "statement": p["statement"], "refs": p["refs"]} for p in patterns]
    for fact in facts:
        fact["claim_type"] = "FACT"
    for hypothesis in hyps:
        hypothesis["claim_type"] = "HYPOTHESIS"
    for action in actions:
        action["claim_type"] = "RECOMMENDATION"
    evidence = [{"claim_type": "EVIDENCE", "relation": "observed",
                 "statement": fact["statement"], "refs": fact["refs"]} for fact in facts]
    for hypothesis in hyps:
        for relation in ("supporting", "contradicting"):
            evidence.extend({"claim_type": "EVIDENCE", "hypothesis": hypothesis["id"],
                             "relation": relation, **item} for item in hypothesis[relation])
    evidence_assessment = {"status": "SUFFICIENT FOR BOUNDED ANALYSIS" if all_ids else "INSUFFICIENT EVIDENCE",
                           "available_evidence": all_ids,
                           "known": [fact["statement"] for fact in facts if fact.get("refs")],
                           "uncertain": [item["what"] for item in unknowns],
                           "recommended_next_evidence": [item["what"] for item in unknowns
                                                         if item.get("kind") == "missing_evidence"]}
    result = {
        "title": problem["title"], "site": "Operator-provided records", "objective": objective,
        "problem": problem, "impact": impact, "facts": facts, "evidence": evidence,
        "evidence_assessment": evidence_assessment,
        "patterns": patterns, "hypotheses": hyps,
        "evidence_chain": [{k: v for k, v in c.items() if k != "time"} | {"time": c["time"].strftime("%H:%M")}
                           for c in chain],
        "unknowns": unknowns, "risks": risks, "actions": actions, "recommendations": actions,
        "opportunities": list(dict.fromkeys(a["opportunity"] for a in actions if a.get("opportunity"))),
        "timeline": timeline, "priority": min((a["priority"] for a in actions), key=PRIORITY_ORDER.get),
        "confidence": top["confidence"], "confidence_method": "heuristic evidence-weight score (see ARCHITECTURE.md)",
        "focus_component": (alt or top).get("component"),
        "input_note": "Analysis is based only on operator-provided records; correlation does not establish "
                      "causation.",
        "records": [{"id": e["id"], "time": _t(e), "label": e["label"], "component": e["comp"]["name"]}
                    for e in events],
        "sources": {"operator_input": {"status": "ok", "source": "operator-provided records",
                                       "records": len(events), "attempts": 1}},
    }
    collected_status = {"operator_input": {"status": "ok", "source": "operator-provided records"}}
    return result, set(all_ids), collected_status
