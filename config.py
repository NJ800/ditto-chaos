"""Shared settings for all scripts.

Every value can be overridden with an environment variable, e.g.
    DITTO_URL=http://192.168.1.20:8080 python probe.py
so teammates can point at the main laptop without editing this file.
"""
import os

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

STATE_DIR = os.getenv("STATE_DIR", "state")
SENT_FILE = os.path.join(STATE_DIR, "sent.json")       # written by simulator, read by probe
METRICS_DIR = os.getenv("METRICS_DIR", "metrics")
BASELINE_FILE = os.getenv("BASELINE_FILE", "baseline.json")

if NUM_CARS < 1 or NUM_CARS > 200:
    raise ValueError("NUM_CARS must be between 1 and 200")


def car_id(index: int) -> str:
    return f"{NAMESPACE}:car-{index:03d}"


def all_car_ids() -> list[str]:
    return [car_id(i) for i in range(1, NUM_CARS + 1)]
