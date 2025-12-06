
#!/usr/bin/env python3
"""
scripts/tune_threshold.py

Unified threshold tuner with optional min/max threshold constraints.
Writes results to ./output/thresholds.json and copies a runtime copy to
./scripts/thresholds.json so the service can load them by default.

Usage (project root):
  python scripts/tune_threshold.py

Environment knobs:
  BETAS           comma list (default "1.0")
  SAMPLE_LIMIT    cap Enron messages (default 50000)
  DATA_WEIGHTS    JSON map of dataset weights (defaults provided)
  MIN_THRESHOLD   lower bound for candidate thresholds (default 0.0)
  MAX_THRESHOLD   upper bound for candidate thresholds (default 1.0)
  PIPELINE_PATH   path to saved pipeline (default models/phishing_model.pkl)
  OUT_PATH        path to write thresholds (default output/thresholds.json)
"""
from __future__ import annotations

import os
import json
import logging
import pickle
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt   # type: ignore[reportMissingImports]
from sklearn.metrics import roc_curve, precision_recall_curve, fbeta_score   # type: ignore[reportMissingImports]

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# ─── Environment knobs ───────────────────────────────────────────────────────
BETAS = [float(b) for b in os.getenv("BETAS", "1.0").split(",")]

try:
    SAMPLE_LIMIT = int(os.getenv("SAMPLE_LIMIT", "50000"))
except Exception:
    logger.warning("Invalid SAMPLE_LIMIT; defaulting to 50000")
    SAMPLE_LIMIT = 50000

DW_RAW = os.getenv("DATA_WEIGHTS", None)
try:
    MIN_T = float(os.getenv("MIN_THRESHOLD", "0.0"))
except Exception:
    MIN_T = 0.0
try:
    MAX_T = float(os.getenv("MAX_THRESHOLD", "1.0"))
except Exception:
    MAX_T = 1.0

logger.info(
    f"BETAS={BETAS} SAMPLE_LIMIT={SAMPLE_LIMIT} "
    f"MIN_THRESHOLD={MIN_T} MAX_THRESHOLD={MAX_T} "
    f"DATA_WEIGHTS={DW_RAW}"
)

# ─── Paths and defaults ─────────────────────────────────────────────────────
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", PROJECT_ROOT / "output")).resolve()
SCRIPTS_DIR = Path(os.getenv("SCRIPTS_DIR", PROJECT_ROOT / "scripts")).resolve()

PIPELINE_PATH = Path(os.getenv("PIPELINE_PATH", PROJECT_ROOT / "models" / "phishing_model.pkl")).resolve()
OUT_PATH_HOST = Path(os.getenv("OUT_PATH", OUTPUT_DIR / "thresholds.json")).resolve()
OUT_PATH_RUNTIME = SCRIPTS_DIR / "thresholds.json"  # copy here for runtime use

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

default_weights = {
    "Enron": 0.6,
    "SpamAssassin": 0.15,
    "PhishTank": 0.15,
    "EnterpriseHybrid": 0.10,
}

# ─── Load & normalize DATA_WEIGHTS ──────────────────────────────────────────
try:
    raw = json.loads(DW_RAW) if DW_RAW else default_weights
    total = sum(float(v) for v in raw.values())
    if total <= 0:
        raise ValueError("sum weights <= 0")
    data_weights = {k: float(v) / total for k, v in raw.items()}
except Exception as e:
    logger.warning("Invalid DATA_WEIGHTS; using defaults: %s", e)
    total = sum(default_weights.values())
    data_weights = {k: float(v) / total for k, v in default_weights.items()}

logger.info("Dataset weights (normalized): %s", data_weights)

# ─── Dataset loaders (import late so script can be run without heavy deps until needed) ───
from api.datasets import (
    load_enron_dataset,
    load_spamassassin_dataset,
    load_phishtank_dataset,
    load_hybrid_enterprise_dataset,
)

DATA_LOADERS = [
    ("Enron", lambda: load_enron_dataset(limit=SAMPLE_LIMIT)),
    ("SpamAssassin", load_spamassassin_dataset),
    ("PhishTank", load_phishtank_dataset),
    ("EnterpriseHybrid", lambda: load_hybrid_enterprise_dataset(real_limit=None, synth_count=None, phish_ratio=0.3)),
]

# ─── Load model ─────────────────────────────────────────────────────────────
if not PIPELINE_PATH.exists():
    raise FileNotFoundError(f"Pipeline not found at {PIPELINE_PATH}")

logger.info("Loading pipeline from %s", PIPELINE_PATH)
try:
    # prefer joblib for sklearn pipelines but keep pickle fallback
    import joblib  # type: ignore
    pipeline = joblib.load(str(PIPELINE_PATH))
except Exception:
    with open(PIPELINE_PATH, "rb") as fh:
        pipeline = pickle.load(fh)

try:
    phishing_idx = list(pipeline.classes_).index("Phishing")
except Exception:
    # fallback to 1 if class ordering unknown
    phishing_idx = 1

# ─── Score each dataset ─────────────────────────────────────────────────────
per_dataset = {}
all_labels = []
all_probs = []

for name, loader in DATA_LOADERS:
    logger.info("Loading %s", name)
    texts, labels = loader()
    logger.info(" %s size: %d", name, len(texts) if texts is not None else 0)
    if not texts:
        logger.info(" skipping %s: empty dataset", name)
        continue
    # ensure pipeline.predict_proba accepts list/iterable of texts
    probs = pipeline.predict_proba(texts)[:, phishing_idx]
    per_dataset[name] = {
        "labels": np.asarray(labels, dtype=int),
        "probs": np.asarray(probs, dtype=float),
        "count": len(labels),
    }
    all_labels.extend(labels)
    all_probs.extend(probs)

if len(all_labels) == 0:
    raise RuntimeError("No data available across loaders to tune thresholds")

all_labels = np.asarray(all_labels, dtype=int)
all_probs = np.asarray(all_probs, dtype=float)

# ─── Compute global ROC & PR curves ────────────────────────────────────────
fpr, tpr, _ = roc_curve(all_labels, all_probs)
prec, rec, thr = precision_recall_curve(all_labels, all_probs)

# thr returned by precision_recall_curve excludes threshold 1.0; ensure unique candidates
thr = np.asarray(thr, dtype=float)
if thr.size == 0:
    # no variable thresholds (e.g., all probs identical); include fallback grid
    thr = np.linspace(max(0.0, MIN_T), min(1.0, MAX_T), num=101)
else:
    # apply min/max threshold filter
    mask = (thr >= MIN_T) & (thr <= MAX_T)
    thr = thr[mask] if (MIN_T > 0.0 or MAX_T < 1.0) else thr
    if thr.size == 0:
        logger.warning("No candidate thresholds after applying MIN/MAX; falling back to trimmed grid")
        thr = np.linspace(max(0.0, MIN_T), min(1.0, MAX_T), num=101)

# ─── Save combined ROC & PR plots into output dir ──────────────────────────
plt.figure(figsize=(8, 6))
plt.plot(fpr, tpr, label="ROC – Combined")
plt.xlabel("False Positive Rate")
plt.ylabel("True Positive Rate")
plt.title("ROC Curve – Combined Datasets")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.savefig(str(OUTPUT_DIR / "roc_curve.png"))
plt.close()

plt.figure(figsize=(8, 6))
plt.plot(rec, prec, label="P–R – Combined")
plt.xlabel("Recall")
plt.ylabel("Precision")
plt.title("Precision–Recall Curve – Combined Datasets")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.savefig(str(OUTPUT_DIR / "pr_curve.png"))
plt.close()

logger.info("Saved roc_curve.png, pr_curve.png to %s", OUTPUT_DIR)

def fbeta_at(y_true, y_probs, t, beta):
    return fbeta_score(y_true, (y_probs >= t).astype(int), beta=beta)

# ─── Optimize thresholds for each β ────────────────────────────────────────
output = {}

for beta in BETAS:
    key = f"F{beta:.1f}"
    logger.info("\nOptimizing %s", key)

    # Global
    fvals = [fbeta_at(all_labels, all_probs, t, beta) for t in thr]
    farr = np.asarray(fvals)
    best_idx = int(np.nanargmax(farr))
    best_thr = float(thr[best_idx])
    best_f = float(fvals[best_idx])
    logger.info(" Global best     = %.4f (F%.1f=%.4f)", best_thr, beta, best_f)

    # WeightedGlobal
    w_scores = []
    for t in thr:
        s = 0.0
        for ds, data in per_dataset.items():
            w = data_weights.get(ds, 0.0)
            if w > 0:
                s += w * fbeta_at(data["labels"], data["probs"], t, beta)
        w_scores.append(s)
    warr = np.asarray(w_scores)
    widx = int(np.nanargmax(warr))
    w_thr = float(thr[widx])
    w_score = float(warr[widx])
    logger.info(" WeightedGlobal best = %.4f (score=%.4f)", w_thr, w_score)

    # PerDataset
    per_ds = {}
    logger.info(" Per-dataset best thresholds (F%.1f):", beta)
    for ds, data in per_dataset.items():
        p, r, tvals = precision_recall_curve(data["labels"], data["probs"])
        if tvals.size == 0:
            chosen = 0.5
        else:
            ds_f = [fbeta_at(data["labels"], data["probs"], t, beta) for t in tvals]
            ds_arr = np.asarray(ds_f)
            idx = int(np.nanargmax(ds_arr))
            chosen = float(tvals[idx])
        per_ds[ds] = chosen
        logger.info("  %15s: threshold=%.4f", ds, chosen)

    output[key] = {"Global": best_thr, "WeightedGlobal": w_thr, "PerDataset": per_ds}

# ─── Write out thresholds.json to output and runtime scripts folder ───────
OUT_DATA = json.dumps(output, indent=2)
OUT_PATH_HOST = OUT_PATH_HOST
with open(OUT_PATH_HOST, "w", encoding="utf-8") as fp:
    fp.write(OUT_DATA)
logger.info("Wrote %s", OUT_PATH_HOST)

# also copy to scripts/thresholds.json so runtime code finds it by default
try:
    with open(OUT_PATH_RUNTIME, "w", encoding="utf-8") as rf:
        rf.write(OUT_DATA)
    logger.info("Copied thresholds to runtime path %s", OUT_PATH_RUNTIME)
except Exception as e:
    logger.warning("Failed to write runtime thresholds to %s: %s", OUT_PATH_RUNTIME, e)

logger.info("\n✅ Wrote thresholds with keys: %s", list(output.keys()))
