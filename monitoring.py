# monitoring.py
"""
Canonical monitoring helpers for project root.
Wrap prometheus_client with safe no-op fallbacks and expose helper functions:
- record_email(risk_score)
- record_export(format, rows)
- record_delete()
- install_metrics_endpoint(app)
This module is placed at project root for easy import by root-level tasks and processes.
"""
from typing import Any, Callable
import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter as _Counter, Histogram as _Histogram
    from prometheus_client import generate_latest as _generate_latest, CONTENT_TYPE_LATEST as _CONTENT_TYPE_LATEST

    PROMETHEUS_AVAILABLE = True
    Counter = _Counter  # type: ignore
    Histogram = _Histogram  # type: ignore
    generate_latest = _generate_latest  # type: ignore
    CONTENT_TYPE_LATEST = _CONTENT_TYPE_LATEST  # type: ignore
except Exception:
    PROMETHEUS_AVAILABLE = False
    def generate_latest() -> bytes:
        return b""
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    class Counter:
        def __init__(self, *a, **k): pass
        def inc(self, *a, **k): pass
    class Histogram:
        def __init__(self, *a, **k): pass
        def observe(self, *a, **k): pass

# Metric names (canonical)
EMAIL_COUNTER = Counter("phishclassifier_emails_processed_total", "Total emails processed")
RISK_HIST = Histogram("phishclassifier_risk_score", "Risk score distribution")
EXPORT_COUNTER = Counter("phishclassifier_reports_export_total", "Total exports generated")
EXPORT_FAILURES = Counter("phishclassifier_reports_export_failures_total", "Total export failures")
DELETE_COUNTER = Counter("phishclassifier_reports_delete_total", "Total report deletions")

def record_email(risk_score: float) -> None:
    try:
        EMAIL_COUNTER.inc()
        if risk_score is not None:
            RISK_HIST.observe(float(risk_score))
    except Exception:
        logger.debug("record_email failed", exc_info=True)

def record_export(fmt: str, rows: Any) -> None:
    try:
        EXPORT_COUNTER.inc()
    except Exception:
        logger.debug("record_export failed", exc_info=True)

def record_delete() -> None:
    try:
        DELETE_COUNTER.inc()
    except Exception:
        logger.debug("record_delete failed", exc_info=True)

def install_metrics_endpoint(app, endpoint: str = "/metrics") -> None:
    from flask import Response  # local import
    @app.route(endpoint)
    def metrics():
        try:
            payload = generate_latest()
            return Response(payload, mimetype=CONTENT_TYPE_LATEST)
        except Exception:
            return Response(b"", mimetype="text/plain")
