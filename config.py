"""Shared settings for all scripts.

Every value can be overridden with an environment variable, e.g.
    DITTO_URL=http://192.168.1.20:8080 python probe.py
so teammates can point at the main laptop without editing this file.

A local .env file (gitignored, see .env.example) is loaded automatically if present --
no python-dotenv dependency needed for a handful of KEY=VALUE lines. Real environment
variables always win over .env, so `DITTO_URL=... python probe.py` still overrides it.
"""
import os


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)  # real env vars still take priority


_load_dotenv()

DITTO_URL = os.getenv("DITTO_URL", "http://localhost:8080").rstrip("/")
DITTO_USER = os.getenv("DITTO_USER", "ditto")
DITTO_PASS = os.getenv("DITTO_PASS", "ditto")

NAMESPACE = os.getenv("NAMESPACE", "org.hackathon")
NUM_CARS = int(os.getenv("NUM_CARS", "100"))          # keep <= 200 (one search page)
SEND_INTERVAL_S = float(os.getenv("SEND_INTERVAL_S", "2.0"))
PROBE_INTERVAL_S = float(os.getenv("PROBE_INTERVAL_S", "2.0"))
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "5.0"))

# A twin in search is "drifted" if it is more than this many updates behind the direct read.
SEARCH_DRIFT_TOLERANCE = int(os.getenv("SEARCH_DRIFT_TOLERANCE", "2"))

# Kill-switch limits for probe.check_breach(). Tune these after you've recorded a real
# baseline (python probe.py --baseline) and watched a few healthy runs.
MAX_ERROR_RATE = float(os.getenv("MAX_ERROR_RATE", "0.05"))              # 5% of requests failing
MAX_LATENCY_FACTOR = float(os.getenv("MAX_LATENCY_FACTOR", "3.0"))       # p95 > 3x baseline p95
MIN_LATENCY_FLOOR_MS = float(os.getenv("MIN_LATENCY_FLOOR_MS", "200"))   # ignore tiny baselines
MAX_STALE_S = float(os.getenv("MAX_STALE_S", "15"))                      # seconds

STATE_DIR = os.getenv("STATE_DIR", "state")
SENT_FILE = os.path.join(STATE_DIR, "sent.json")       # written by simulator, read by probe
METRICS_DIR = os.getenv("METRICS_DIR", "metrics")
BASELINE_FILE = os.getenv("BASELINE_FILE", "baseline.json")
EXPERIMENTS_DIR = os.getenv("EXPERIMENTS_DIR", "experiments")
REPORTS_DIR = os.getenv("REPORTS_DIR", "reports")
DEPENDENCY_MAP_DIR = os.getenv("DEPENDENCY_MAP_DIR", ".")

# Optional AI analysis (resilience_agent.py). Entirely optional: every AI-touching
# module must keep working -- with a clearly-labeled deterministic fallback, never a
# crash or a fabricated result -- when this is disabled or no key is configured.
AI_ENABLED = os.getenv("AI_ENABLED", "false").strip().lower() in ("1", "true", "yes")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
AI_REQUEST_TIMEOUT_S = float(os.getenv("AI_REQUEST_TIMEOUT_S", "30"))

# api.py (dashboard backend). Binds to localhost by default -- this is a local
# development tool with destructive endpoints behind it; do not expose API_HOST to
# an untrusted network without adding real authentication first (see README).
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
# Comma-separated list of origins the dashboard (frontend/) is allowed to call the
# API from. The Vite dev server defaults to 5173.
FRONTEND_ORIGINS = [o.strip() for o in
                    os.getenv("FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
                    if o.strip()]

if NUM_CARS < 1 or NUM_CARS > 200:
    raise ValueError("NUM_CARS must be between 1 and 200")


def car_id(index: int) -> str:
    return f"{NAMESPACE}:car-{index:03d}"


def all_car_ids() -> list[str]:
    return [car_id(i) for i in range(1, NUM_CARS + 1)]
