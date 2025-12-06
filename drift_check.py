# drift_check.py
"""
Nightly drift check: compare recent feature histograms to baseline.
Writes a simple alert file if significant drift is detected.
This is a lightweight scaffold; wire it to a scheduler (cron, systemd timer, or GitHub Actions).
"""

import os
import json
import logging
import time
from collections import Counter
from statistics import mean
from typing import List

BASE_DIR = os.path.dirname(__file__)
DRIFT_DIR = os.path.join(BASE_DIR, "drift")
BASELINE_FILE = os.path.join(DRIFT_DIR, "baseline_stats.json")
RECENT_FILE = os.path.join(DRIFT_DIR, "recent_stats.json")
ALERT_FILE = os.path.join(DRIFT_DIR, "drift_alert.json")

os.makedirs(DRIFT_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

def summarize_feature(values: List[float]) -> dict:
    if not values:
        return {"count": 0, "mean": 0.0}
    return {"count": len(values), "mean": float(mean(values))}

def compute_recent_stats(risk_scores: List[float]) -> dict:
    return {"risk": summarize_feature(risk_scores)}

def load_baseline() -> dict:
    if not os.path.exists(BASELINE_FILE):
        return {}
    with open(BASELINE_FILE, "r") as fh:
        return json.load(fh)

def save_baseline(stats: dict):
    with open(BASELINE_FILE, "w") as fh:
        json.dump(stats, fh)

def run_drift_check(recent_risk_scores: List[float]):
    baseline = load_baseline()
    recent_stats = compute_recent_stats(recent_risk_scores)
    with open(RECENT_FILE, "w") as fh:
        json.dump(recent_stats, fh)

    # if no baseline, initialize it
    if not baseline:
        logging.info("No baseline found; initializing baseline with recent stats")
        save_baseline(recent_stats)
        return

    # simple threshold: mean risk change > 0.15 triggers alert
    base_mean = baseline.get("risk", {}).get("mean", 0.0)
    recent_mean = recent_stats.get("risk", {}).get("mean", 0.0)
    if abs(recent_mean - base_mean) > 0.15:
        alert = {
            "baseline_mean": base_mean,
            "recent_mean": recent_mean,
            "delta": recent_mean - base_mean,
            "time": time.time()
        }
        with open(ALERT_FILE, "w") as fh:
            json.dump(alert, fh)
        logging.warning("Drift detected: %s", alert)
    else:
        logging.info("No significant drift: baseline=%.3f recent=%.3f", base_mean, recent_mean)
