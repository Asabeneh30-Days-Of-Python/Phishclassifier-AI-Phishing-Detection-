# api/routes.py
"""
API blueprint for prediction endpoints: home, predict, and batch_predict.
Tolerant body parsing, language normalization, defensive persistence of QuarantinedEmail,
and clear JSON responses for client consumption.
"""

from flask import Blueprint, jsonify, request, render_template, current_app, send_file
import json
import logging
from typing import Dict, Any, Optional
import uuid
from hashlib import sha256
from datetime import datetime
import os

log = logging.getLogger(__name__)

prediction = Blueprint(
    'prediction',
    __name__,
    template_folder='templates'
)

LANG_NORMALIZATION: Dict[str, str] = {
    "en": "en",
    "en-US": "en",
    "en-GB": "en",
}


def _short_hash(s: str, length=12):
    return sha256((s or "").encode("utf-8")).hexdigest()[:length]


def _normalize_lang_in_dict(d: Dict[str, Any]) -> None:
    if not isinstance(d, dict):
        return
    for key in ("lang", "language", "locale"):
        if key in d and isinstance(d.get(key), str):
            raw = d.get(key).strip()
            if raw:
                d[key] = LANG_NORMALIZATION.get(raw, raw)
            break


def _get_body_dict() -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    try:
        if request.is_json:
            body = request.get_json(silent=True) or {}
        else:
            body = request.form.to_dict() or {}
            if not body:
                raw = request.get_data(as_text=True) or ""
                if raw:
                    try:
                        body = json.loads(raw)
                    except Exception:
                        body = {}
    except Exception:
        log.exception("Error parsing request body")
        body = {}

    try:
        _normalize_lang_in_dict(body)
    except Exception:
        log.exception("Language normalization failed for parsed body: %s", body)

    return body


@prediction.route('/', methods=['GET'])
def home():
    return render_template('index.html')


@prediction.route('/predict', methods=['POST'])
def predict():
    """
    Process a single email prediction, attempt to persist a QuarantinedEmail
    row (best-effort), and return a normalized JSON prediction result.
    Expected body keys: 'email'|'text'|'content' for the message; optional 'sender', 'subject'.
    """
    data = _get_body_dict()
    email_text = (data.get('email') or data.get('text') or data.get('content') or "").strip()

    if not email_text:
        return jsonify({"error": "Email content is required."}), 400

    try:
        from api.classifier import classify_text
        from api.database import db

        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        current_app.logger.info("predict:start cid=%s preview_hash=%s", cid, _short_hash(email_text[:256]))

        result = classify_text(email_text)
        if not isinstance(result, dict):
            raise ValueError("Unexpected classifier return type")

        result["risk_score"] = float(result.get("risk_score") or 0.0)
        result["risk_score_percent"] = float(result.get("risk_score_percent") or (result["risk_score"] * 100.0))
        result["probability"] = float(result.get("probability") or result["risk_score"])

        result["model_version"] = result.get("model_version") or "v1.0"

        # --- begin: auto-write artifact into REPORT_PATH and attach to TaskAudit (non-fatal) ---
        try:
            import uuid as _uuid, json as _json
            from api.reports import _generate_and_write_report
            from models.task_audit import TaskAudit

            # choose stem: prefer X-Correlation-ID header if present; else short uuid
            cid_local = cid or request.headers.get("X-Correlation-ID") or str(_uuid.uuid4())
            # make safe and short
            stem = str(cid_local).replace("/", "_")[:64]

            # body content to write: prefer original email_text (easy for UI)
            body_to_write = email_text if email_text else (result.get("prediction") if isinstance(result, dict) else str(result))

            # atomically write body and optional full JSON result
            written = None
            try:
                written = _generate_and_write_report(stem, body_to_write or "", suffix=".body.txt")
                # write a fuller JSON file for operators (optional)
                _generate_and_write_report(stem, _json.dumps(result, default=str, ensure_ascii=False), suffix=".full.body.txt")
            except Exception:
                current_app.logger.exception("reports: failed to write artifact (non-fatal)")

            # best-effort attach artifact info to a TaskAudit row so UI will render card
            try:
                # prefer TaskAudit.task_id == cid if such linkage exists, else most recent row
                ta = None
                try:
                    ta = db.session.query(TaskAudit).filter(TaskAudit.task_id == cid_local).order_by(TaskAudit.updated_at.desc()).first()
                except Exception:
                    ta = None
                if not ta:
                    ta = db.session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).first()

                if ta and written:
                    # attach result_meta dict (UI accepts persistence_id OR filename)
                    rm = ta.result_meta if ta.result_meta else {}
                    try:
                        if isinstance(rm, str):
                            rm = _json.loads(rm)
                    except Exception:
                        rm = {"__raw": str(rm)[:400]}
                    rm.update({"persistence_id": stem, "filename": written, "full_filename": f"{stem}.full.body.txt"})
                    ta.result_meta = rm
                    # if numeric persistence_id column exists and stem is numeric, set it
                    try:
                        if hasattr(ta, "persistence_id") and str(stem).isdigit():
                            ta.persistence_id = int(stem)
                    except Exception:
                        pass
                    db.session.commit()
            except Exception:
                current_app.logger.exception("auto-attach artifact failed (non-fatal)")
        except Exception:
            current_app.logger.exception("auto-attach artifact failed (non-fatal)")
        # --- end ---

        return jsonify(result), 200

    except Exception as e:
        log.exception("Prediction failed for input: %s", email_text[:200])
        return jsonify({"error": "Prediction failed", "details": str(e)}), 500


@prediction.route('/batch_predict', methods=['POST'])
def batch_predict():
    """
    Accepts: {"emails": ["text1", "text2", ...]}
    Returns: list of normalized prediction dicts
    """
    data = _get_body_dict() or {}
    emails = data.get('emails')

    if not isinstance(emails, list) or not emails:
        return jsonify({"error": "Please provide a non-empty list of emails."}), 400

    results = []
    try:
        from api.classifier import classify_text

        for text in emails:
            text_str = (text or "")
            if not isinstance(text_str, str):
                text_str = str(text_str)
            r = classify_text(text_str)
            if not isinstance(r, dict):
                r = {"prediction": None, "risk_score": 0.0, "probability": 0.0}
            try:
                r["risk_score"] = float(r.get("risk_score") or 0.0)
            except Exception:
                r["risk_score"] = 0.0
            try:
                r["risk_score_percent"] = float(r.get("risk_score_percent") or (r["risk_score"] * 100.0))
            except Exception:
                r["risk_score_percent"] = r["risk_score"] * 100.0
            try:
                r["probability"] = float(r.get("probability") or r["risk_score"])
            except Exception:
                r["probability"] = r["risk_score"]

            results.append(r)

        return jsonify(results), 200

    except Exception as e:
        log.exception("Batch prediction failed")
        return jsonify({"error": "Batch prediction failed", "details": str(e)}), 500


@prediction.route('/_debug/raw_report/<id>', methods=['GET'])
def _debug_raw_report(id):
    """
    Development-only endpoint to return the raw report body file contents.
    Will look up REPORT_PATH from app config then environment, falling back to /tmp/phish_reports.
    Remove this route after verification.
    """
    path = None
    try:
        path = current_app.config.get("REPORT_PATH")
    except Exception:
        path = os.environ.get("REPORT_PATH")
    if not path:
        path = "/tmp/phish_reports"
    filename = f"{id}.full.body.txt"
    fullpath = os.path.join(path, filename)
    if os.path.exists(fullpath):
        try:
            return send_file(fullpath, mimetype="text/plain")
        except Exception:
            current_app.logger.exception("debug_raw_report: failed to send file %s", fullpath)
            return ("internal error", 500)
    return ("not found", 404)
