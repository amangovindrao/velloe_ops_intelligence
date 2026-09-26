"""
Fault injector used to break the system on purpose and watch it recover.

Spec string: "telemetry=500+timeout+malformed,weather=down,intelligence=hallucinate"
Each endpoint has a queue of faults consumed one per attempt; "down" is sticky
(every attempt fails). rate > 0 adds random 500/timeout/malformed faults on
top, which is what benchmark.py uses.

Endpoints: incidents, telemetry, maintenance (primary Ops API),
snapshot:<endpoint> (fallback export), weather (Open-Meteo), weather_cache,
intelligence (LLM output -> "hallucinate" injects a fabricated citation,
"overclaim" states correlation as proven causation).
"""
import random

FAULT_TYPES = ("500", "429", "timeout", "malformed", "schema", "no_data", "down",
               "hallucinate", "overclaim")
RANDOM_FAULTS = ("500", "timeout", "malformed")

# Judge-facing "Simulation Mode" options. {tool} = the first data source in the plan.
UI_FAILURES = {
    "normal": ("Normal", ""),
    "api_500": ("API 500", "{tool}=500+500+500"),
    "timeout": ("Timeout", "{tool}=timeout+timeout+timeout"),
    "malformed": ("Malformed Response", "{tool}=malformed+malformed+malformed"),
    "no_data": ("No Data", "{tool}=no_data+no_data+no_data"),
    "outage": ("Primary + fallback down", "{tool}=down,snapshot:{tool}=down"),
    "hallucination": ("LLM hallucinated citation", "intelligence=hallucinate"),
    "overclaim": ("LLM overclaims causation", "intelligence=overclaim"),
}
# Preserve old callers without showing duplicate options in the dashboard.
UI_FAILURE_ALIASES = {"none": "normal", "api500": "api_500"}
# Which simulation modes make sense for each agent in Individual Agent Mode.
AGENT_FAILURES = {
    "strategist": ("normal", "none"),
    "scout": ("normal", "none", "api_500", "api500", "timeout", "malformed", "no_data", "outage"),
    "intelligence": ("normal", "none", "hallucination", "overclaim"),
    "guardian": ("normal", "none"),
}

# Named CLI scenarios (run_demo.py --scenario).
SCENARIOS = {
    "clean": "",
    "demo": "telemetry=500+timeout+malformed,intelligence=hallucinate",
    "outage": "incidents=down,telemetry=down,maintenance=down,weather=down",
    "judge": "incidents=500+timeout+malformed",
    "blackout": "telemetry=down,snapshot:telemetry=down,weather=down,weather_cache=down",
}


class FaultInjector:
    def __init__(self, spec="", rate=0.0, seed=None):
        self.spec = spec or ""
        self.queues, self.sticky = {}, set()
        self.rate = rate
        self.rng = random.Random(seed)
        self.fired = []
        for part in filter(None, (p.strip() for p in self.spec.split(","))):
            endpoint, _, faults = part.partition("=")
            for f in filter(None, faults.split("+")):
                if f not in FAULT_TYPES:
                    raise ValueError(f"unknown fault '{f}' for '{endpoint}'")
                if f == "down":
                    self.sticky.add(endpoint)
                else:
                    self.queues.setdefault(endpoint, []).append(f)
        self._template = {k: list(v) for k, v in self.queues.items()}  # pristine copy for scoped queues

    def next(self, endpoint, scope=None):
        """Fault to apply to this attempt on `endpoint`, or None.
        `scope` (e.g. a site code) gives each scope its own copy of the endpoint's fault queue, so
        'weather=500+500+500' fails every site's weather call instead of only the first site's."""
        fault = None
        key = endpoint
        if scope is not None:
            key = f"{endpoint}@{scope}"
            if key not in self.queues and endpoint in self._template:
                self.queues[key] = list(self._template[endpoint])
        if endpoint in self.sticky or key in self.sticky:
            fault = "down"
        elif self.queues.get(key):
            fault = self.queues[key].pop(0)
        elif self.rate and endpoint in ("incidents", "telemetry", "maintenance", "weather") \
                and self.rng.random() < self.rate:
            fault = self.rng.choice(RANDOM_FAULTS)
        if fault:
            self.fired.append((endpoint, fault))
        return fault


def build(scenario_or_spec="", rate=0.0, seed=None):
    spec = SCENARIOS.get(scenario_or_spec, scenario_or_spec or "")
    return FaultInjector(spec, rate=rate, seed=seed)


def ui_failure_spec(mode, tool):
    mode = UI_FAILURE_ALIASES.get(mode, mode)
    if mode not in UI_FAILURES:
        raise ValueError(f"unknown failure mode '{mode}'")
    spec = UI_FAILURES[mode][1].format(tool=tool)
    return spec.replace("snapshot:weather", "weather_cache")  # weather's fallback endpoint name
