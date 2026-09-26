"""
Central settings for Velloe Ops Intelligence. Every value can be overridden by
an environment variable or a line in .env (real env vars win).
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path=os.path.join(ROOT, ".env")):
    """Tiny .env loader (no extra dependency)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# --- Identity -------------------------------------------------------------
PRODUCT = "Velloe Ops Intelligence"
# Four specialized agents + a non-AI orchestration layer.
AGENT_NAMES = {
    "orchestrator": "Orchestrator",
    "strategist": "Velloe Strategist",
    "scout": "Velloe Scout",
    "intelligence": "Velloe Intelligence",
    "guardian": "Velloe Guardian",
    "human": "Human Approver",
}
AGENTS = ("strategist", "scout", "intelligence", "guardian")

# --- Paths ----------------------------------------------------------------
OPS_DATA_PATH = os.environ.get("OPS_DATA_PATH", os.path.join(ROOT, "data", "velloe_ops_snapshot.json"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(ROOT, "output"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(OUTPUT_DIR, "velloe_ops.db"))

# --- LLM ------------------------------------------------------------------
# MOCK_MODE: "auto" (mock if no provider configured), "on" (always mock), "off"
MOCK_MODE = os.environ.get("MOCK_MODE", "auto")
# LLM_PROVIDER: "auto" | "openai_compatible" (OmniRoute, Ollama, LM Studio...) | "anthropic"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "auto")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", 90))
# Fallback OpenAI-compatible endpoint (e.g. Google Gemini free tier), tried when the primary fails.
# LLM_FALLBACK_MODEL may list several models, comma separated, tried in order.
LLM_FALLBACK_BASE_URL = os.environ.get("LLM_FALLBACK_BASE_URL", "").rstrip("/")
LLM_FALLBACK_API_KEY = os.environ.get("LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "")
LLM_FALLBACK_TIMEOUT_SECONDS = float(os.environ.get("LLM_FALLBACK_TIMEOUT_SECONDS", 20))
# After a provider fails, skip it for this long so every call does not wait for the same dead upstream.
LLM_COOLDOWN_SECONDS = float(os.environ.get("LLM_COOLDOWN_SECONDS", 120))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# --- Orchestrator limits (stopping conditions) ----------------------------
# Canonical judge-facing names take precedence; legacy names remain supported.
MAX_WORKFLOW_STEPS = int(os.environ.get("MAX_WORKFLOW_STEPS", os.environ.get("MAX_STEPS", 120)))
EXECUTION_TIMEOUT = float(os.environ.get("EXECUTION_TIMEOUT", os.environ.get("MAX_SECONDS", 300)))
MAX_GUARDIAN_REVISIONS = int(os.environ.get("MAX_GUARDIAN_REVISIONS", os.environ.get("MAX_REVISIONS", 2)))
MAX_LLM_CALLS = int(os.environ.get("MAX_LLM_CALLS", 12))
MAX_STEPS = MAX_WORKFLOW_STEPS
MAX_SECONDS = EXECUTION_TIMEOUT
MAX_REVISIONS = MAX_GUARDIAN_REVISIONS

# Caller deadline for one agent in Individual Agent Mode. Python threads cannot be force-killed.
AGENT_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TIMEOUT_SECONDS", 60))

# --- Scout reliability ----------------------------------------------------
# Canonical retries exclude the initial call; legacy attempts include it.
if "MAX_TOOL_RETRIES" in os.environ:
    MAX_TOOL_RETRIES = int(os.environ["MAX_TOOL_RETRIES"])
    SCOUT_MAX_ATTEMPTS = MAX_TOOL_RETRIES + 1
else:
    SCOUT_MAX_ATTEMPTS = int(os.environ.get("SCOUT_MAX_ATTEMPTS", 3))
    MAX_TOOL_RETRIES = max(0, SCOUT_MAX_ATTEMPTS - 1)
SCOUT_TIMEOUT_SECONDS = float(os.environ.get("SCOUT_TIMEOUT_SECONDS", 1.5))
WEATHER_TIMEOUT_SECONDS = float(os.environ.get("WEATHER_TIMEOUT_SECONDS", 8))
SCOUT_BACKOFF_SECONDS = float(os.environ.get("SCOUT_BACKOFF_SECONDS", 0.3))
SIM_API_LATENCY_SECONDS = float(os.environ.get("SIM_API_LATENCY_SECONDS", 0.05))

# --- Analysis -------------------------------------------------------------
THERMAL_THRESHOLD_C = float(os.environ.get("THERMAL_THRESHOLD_C", 27.0))  # ASHRAE recommended max inlet

# --- Web dashboard --------------------------------------------------------
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
# Pause after each audit entry when running from the dashboard so viewers can follow along.
DEMO_STEP_DELAY_SECONDS = float(os.environ.get("DEMO_STEP_DELAY_SECONDS", 0.25))
