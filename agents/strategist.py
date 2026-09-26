"""
Velloe Strategist - "What should we do?"

Understands the request, picks the problem domain, scope (sites + time window)
and which data sources the Scout must collect, and writes a delegation plan.
Deterministic rules produce a baseline plan; if a real LLM is configured it may
choose a different domain/tool set, which is validated against the tool catalog
(invalid LLM output falls back to the rule plan).
"""
import json
import re
from datetime import datetime

from llm_client import call_llm
from tools.ops_api import dataset_window, site_registry

TOOLS = {
    "incidents": "Incident records (Velloe Ops API; fallback: snapshot export)",
    "telemetry": "Sensor telemetry, e.g. rack inlet temperature (Ops API; fallback: snapshot export)",
    "maintenance": "Maintenance and change log (Ops API; fallback: snapshot export)",
    "weather": "Historical outdoor weather for the site (Open-Meteo API; fallback: cached export)",
}
DOMAINS = {
    "thermal": ["temperature", "thermal", "cooling", "hvac", "heat", "crac", "hot", "inlet", "overheat"],
    "network": ["network", "outage", "flap", "flapping", "packet", "switch", "latency", "vlan", "link", "connectivity"],
    "power": ["power", "ups", "battery", "grid", "voltage", "generator", "pdu"],
    "change": ["change", "firmware", "maintenance", "upgrade", "patch", "deploy"],
}
DOMAIN_TOOLS = {
    "thermal": ["telemetry", "incidents", "weather"],
    "network": ["incidents", "telemetry", "maintenance"],
    "power": ["incidents", "weather"],
    "change": ["maintenance", "incidents", "telemetry"],
    "general": ["incidents", "telemetry", "maintenance"],
}
DOMAIN_LABEL = {"thermal": "the temperature / cooling anomaly", "network": "the repeated network incidents",
                "power": "the power events", "change": "the change-related problems", "general": "the reported problem"}
DOMAIN_STEPS = {
    "thermal": ["Retrieve rack inlet temperature telemetry", "Identify periods above the thermal threshold",
                "Retrieve thermal and related incidents", "Retrieve outdoor weather for the site",
                "Compare timestamps across sources", "Identify recurring patterns", "Determine missing evidence",
                "Recommend next investigation steps"],
    "network": ["Retrieve recent network incidents", "Retrieve environmental telemetry for the affected rows",
                "Check maintenance and change history", "Compare timestamps across sources",
                "Identify recurring patterns", "Determine missing evidence", "Recommend next investigation steps"],
    "power": ["Retrieve recent power events and UPS transfer warnings", "Retrieve external weather context",
              "Compare recurring timestamps", "Identify missing UPS diagnostics and maintenance evidence",
              "Identify recurring patterns", "Determine missing evidence", "Recommend next investigation steps"],
    "change": ["Retrieve maintenance and change records", "Retrieve incidents after each change",
               "Retrieve telemetry around each change window", "Compare timestamps",
               "Identify recurring patterns", "Determine missing evidence", "Recommend next investigation steps"],
    "general": ["Retrieve recent incidents", "Retrieve telemetry", "Check maintenance history",
                "Compare timestamps", "Identify recurring patterns", "Determine missing evidence",
                "Recommend next investigation steps"],
}
DATA_NEEDED = {"incidents": "Incident / event logs", "telemetry": "Sensor telemetry (inlet temperature)",
               "maintenance": "Maintenance and change history", "weather": "Historical outdoor weather"}
EXTRA_DATA = {"power": ["UPS alerts (recorded as power incidents)",
                              "UPS diagnostic and maintenance records (requested during verification if needed)"]}
TOOL_NAMES = {"incidents": "Incident API", "telemetry": "Telemetry API", "maintenance": "Maintenance API",
              "weather": "Open-Meteo weather API"}
DOMAIN_UNKNOWNS = {
    "thermal": ["HVAC/CRAC controller telemetry is not connected to the Ops data."],
    "network": ["Device syslog and interface counters are not connected to the Ops data."],
    "power": ["Power-quality measurements (voltage, frequency) may not be available.",
              "UPS diagnostic logs are not connected to the Ops data."],
    "change": ["Change records may not include exact rollout times per device."],
    "general": ["The cause category is unclear; the plan casts a wide net."],
}
REASONS = {
    "incidents": "Establish what was reported, when, and how severe.",
    "telemetry": "Measure the physical signal (inlet temperature) and find anomalies.",
    "maintenance": "Check whether planned work or a change coincides with the problem.",
    "weather": "Test whether external heat is a plausible contributing factor.",
}

SYSTEM = (
    "You are Velloe Strategist, the planning agent of an infrastructure operational-intelligence "
    "system. You decide which data sources are needed to investigate an incident. You never "
    "invent data. Answer with JSON only."
)


def kw(term, text):
    """Keyword at a word start. Short terms (<5 chars) only take plural/verb endings ('ups' must not match
    'backups', 'hot' not 'photo'/'hotfix'); longer terms take any ending ('outages', 'patches', not 'dispatch')."""
    tail = r"\w*" if len(term) >= 5 else r"(?:s|es|ed|ing)?\b"
    return re.search(rf"\b{re.escape(term)}{tail}", text) is not None


def _detect_domain(question):
    q = question.lower()
    scores = {d: sum(kw(k, q) for k in kws) for d, kws in DOMAINS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "general"


def _detect_sites(question, registry):
    q = question.lower()
    return [code for code, s in registry.items()
            if code.lower() in q or any(re.search(rf"\b{re.escape(a)}\b", q) for a in s.get("aliases", []))]


def _valid_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _detect_window(question):
    # impossible dates such as 2024-06-31 are ignored (they crashed analysis and the weather request)
    dates = [d for d in re.findall(r"\b(20\d\d-\d\d-\d\d)\b", question) if _valid_date(d)]
    window = dataset_window()
    if len(dates) >= 2:
        window = {"start": min(dates), "end": max(dates)}
    elif len(dates) == 1:
        window = {"start": dates[0], "end": dates[0]}
    return window


def _tasks(tools, domain=None):
    reasons = dict(REASONS)
    if domain == "power":
        reasons["weather"] = ("Capture external weather context with a genuine public API; it is contextual "
                              "evidence only and is never treated as proof of an electrical cause.")
    return [{"id": f"T{i + 1}", "tool": t, "agent": "scout", "reason": reasons[t]}
            for i, t in enumerate(tools)]


def plan(question):
    """Returns (plan_dict, llm_meta)."""
    registry = site_registry()
    domain = _detect_domain(question)
    tools = list(DOMAIN_TOOLS[domain])
    planned_by = "rules"

    prompt = f"""Investigation request: {question}

Available data sources (use only these keys):
{json.dumps(TOOLS, indent=2)}
Domains: thermal, network, power, change, general.

Return JSON: {{"domain": "<domain>", "tools": ["<key>", ...]}} with 2-4 tools, most important first."""
    fallback = json.dumps({"domain": domain, "tools": tools})
    text, meta = call_llm(SYSTEM, prompt, fallback=fallback, max_tokens=200)
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        llm_tools = [t for t in dict.fromkeys(data.get("tools", [])) if t in TOOLS]
        if data.get("domain") in DOMAIN_TOOLS and len(llm_tools) >= 2 and not meta.get("mock"):
            domain, tools, planned_by = data["domain"], llm_tools, "llm"
    except (ValueError, AttributeError, TypeError):
        pass  # keep the rule-based plan (also for type-valid but unexpected LLM JSON, e.g. "tools": null)

    sites = _detect_sites(question, registry) or list(registry)
    labels = [registry.get(s, {}).get("label", s) for s in sites]
    return {
        "objective": question,
        "objective_text": f"Determine possible causes of {DOMAIN_LABEL[domain]} at {', '.join(labels)}.",
        "steps": DOMAIN_STEPS[domain],
        "required_data": [DATA_NEEDED[t] for t in tools] + EXTRA_DATA.get(domain, []),
        "potential_tools": [TOOL_NAMES[t] for t in tools],
        "risks": DOMAIN_UNKNOWNS[domain] + (["Operational records are simulated demo data."]),
        "domain": domain,
        "sites": sites,
        "site_labels": labels,
        "window": _detect_window(question),
        "tasks": _tasks(tools, domain),
        "planned_by": planned_by,
        "delegation": [
            {"agent": "scout", "work": f"collect {', '.join(tools)} with retry/timeout/fallback"},
            {"agent": "intelligence", "work": "detect patterns, rank hypotheses, recommend actions"},
            {"agent": "guardian", "work": "verify evidence, reject weak or unsupported results"},
        ],
    }, meta


def add_tasks(plan_dict, tools):
    """Extend the plan with extra data sources (used when the Guardian asks for more evidence)."""
    have = {t["tool"] for t in plan_dict["tasks"]}
    new = []
    for tool in tools:
        if tool in TOOLS and tool not in have:
            task = {"id": f"T{len(plan_dict['tasks']) + 1}", "tool": tool, "agent": "scout",
                    "reason": REASONS[tool] + " (requested during verification)"}
            plan_dict["tasks"].append(task)
            new.append(task)
            have.add(tool)
    return new
