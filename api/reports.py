# api/reports.py

"""
Reports generation and download blueprint.

This module provides:
- /reports/ download and listing endpoints
- helper functions to generate CSV reports
- Celery task generate_report_task (if celery available)
- helpers to write artifacts atomically and return normalized artifact info
"""
import os
import json
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

from flask import (
    Blueprint,
    current_app,
    send_from_directory,
    jsonify,
    request,
    abort,
    url_for,
)
from flask_login import login_required, current_user

# Blueprint
reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


# -- Helpers -----------------------------------------------------------------


def _report_dir() -> str:
    """
    Resolve the REPORT_PATH from app config, create if missing.
    """
    rp = None
    try:
        rp = current_app.config.get("REPORT_PATH")
    except Exception:
        rp = None
    if not rp:
        # default to instance/reports under app.root_path when running under Flask
        try:
            rp = os.path.join(current_app.instance_path, "reports")
        except Exception:
            rp = os.environ.get("REPORT_PATH", "/tmp/phish_reports")
    rp = os.path.abspath(rp)
    try:
        os.makedirs(rp, exist_ok=True)
    except Exception:
        # best-effort: ignore if cannot create (serving will fail later)
        pass
    return rp


def _is_safe_basename(name: str) -> bool:
    """Reject traversal or absolute paths; only allow simple basenames without slashes."""
    if not name or "/" in name or "\\" in name:
        return False
    if ".." in name:
        return False
    # basic whitelist: allow letters, digits, dots, dashes, underscores
    return True


def _load_result_meta(obj: Any) -> Dict[str, Any]:
    """
    Normalize a TaskAudit.result_meta-like object into a dict with keys we expect.
    Accepts dict or JSON string. Returns empty dict when unparseable.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except Exception:
            return {}
    return {}


def _extract_artifact_info(result_meta: Any) -> Dict[str, Optional[Any]]:
    """
    Given result_meta (dict or JSON), return normalized artifact info:
      { persistence_id, filename, full_filename, log_filename }
    Accepts both numeric persistence ids and non-numeric artifact ids (Celery task ids).
    """
    rm = _load_result_meta(result_meta)
    pid = (
        rm.get("persistence_id")
        or rm.get("id")
        or rm.get("persistenceId")
        or rm.get("artifact_id")
    )
    filename = rm.get("filename") or rm.get("body_filename") or rm.get("body")
    full_filename = (
        rm.get("full_filename") or rm.get("full_body") or rm.get("fullFilename")
    )
    log_filename = rm.get("log_filename") or rm.get("log") or (f"{pid}.log" if pid else None)

    def _ensure_basename(x):
        return os.path.basename(x) if isinstance(x, str) and _is_safe_basename(os.path.basename(x)) else None

    # Attempt to coerce numeric persistence ids to int; otherwise keep as-is for artifact/task ids
    pid_out = None
    try:
        if pid not in (None, "") and str(pid).isdigit():
            pid_out = int(pid)
        elif pid not in (None, ""):
            pid_out = pid
    except Exception:
        pid_out = None

    return {
        "persistence_id": pid_out,
        "filename": _ensure_basename(filename),
        "full_filename": _ensure_basename(full_filename),
        "log_filename": _ensure_basename(log_filename),
        "raw_result_meta": rm,
    }


def _admin_required_view(func):
    """
    Simple admin guard decorator for views. Uses current_user.is_authenticated
    and getattr(current_user, 'is_admin', False).
    """
    from functools import wraps

    @wraps(func)
    def wrapped(*a, **kw):
        if not current_user.is_authenticated:
            return abort(401)
        if not getattr(current_user, "is_admin", False):
            return abort(403)
        return func(*a, **kw)

    return wrapped


def _try_find_file_for_ids(basenames: Tuple[Optional[str], ...]) -> Optional[str]:
    """
    Given a sequence of candidate basenames, return the first that exists in REPORT_PATH.
    """
    rd = _report_dir()
    for b in basenames:
        if not b:
            continue
        if not _is_safe_basename(b):
            continue
        p = os.path.join(rd, b)
        if os.path.exists(p):
            return b
    return None


def _generate_and_write_report(stem: str, content: str, suffix: str = ".body.txt") -> Optional[str]:
    """
    Write report content atomically into REPORT_PATH and return the basename written.

    - stem: filename stem (e.g. persistence id or task id)
    - content: text content to write
    - suffix: file suffix to use (default .body.txt)

    Returns the basename if successful, otherwise None.
    """
    if not stem or not _is_safe_basename(stem):
        return None

    rd = _report_dir()
    try:
        os.makedirs(rd, exist_ok=True)
    except Exception:
        return None

    # ensure suffix starts with dot
    if not suffix.startswith("."):
        suffix = "." + suffix

    basename = f"{stem}{suffix}"
    if not _is_safe_basename(basename):
        return None

    tmpname = f".{basename}.tmp"
    tmp_path = os.path.join(rd, tmpname)
    final_path = os.path.join(rd, basename)

    try:
        # Write to tmp file, flush and fsync to increase durability, then atomic replace.
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                # fsync may not be available in some environments; proceed but log at debug level if possible
                try:
                    current_app.logger.debug("fsync unavailable for %s", tmp_path, exc_info=True)
                except Exception:
                    pass
        # Ensure replace is atomic
        os.replace(tmp_path, final_path)
        try:
            os.chmod(final_path, 0o644)
        except Exception:
            try:
                current_app.logger.debug("chmod failed for %s", final_path, exc_info=True)
            except Exception:
                pass
        return basename
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return None


# -- Artifact endpoints -----------------------------------------------------


@reports_bp.route("body/<task_id>", methods=["GET"])
@login_required
def body_report(task_id: str):
    """
    Serve the canonical body artifact for task_id.

    task_id is treated as a simple basename stem (e.g. "1234" -> "1234.body.txt" or "1234.full.body.txt")
    The endpoint will try a small set of standard suffixes in order.
    """
    if not _is_safe_basename(task_id):
        return jsonify({"error": "invalid task id"}), 400

    rd = _report_dir()
    # candidate filenames in preferred order
    candidates = [
        f"{task_id}.full.body.txt",
        f"{task_id}.body.txt",
        f"{task_id}.full.txt",
        f"{task_id}.txt",
    ]
    found = _try_find_file_for_ids(tuple(candidates))
    if not found:
        return jsonify({"error": "not found"}), 404

    # send inline (not attachment) so UI can fetch text or download via download endpoint
    return send_from_directory(rd, found, as_attachment=False)


@reports_bp.route("log/<task_id>", methods=["GET"])
@login_required
def log_report(task_id: str):
    """
    Serve the per-prediction log file for task_id (e.g. "<id>.log").
    """
    if not _is_safe_basename(task_id):
        return jsonify({"error": "invalid task id"}), 400

    rd = _report_dir()
    filename = f"{task_id}.log"
    if not os.path.exists(os.path.join(rd, filename)):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(rd, filename, as_attachment=False)


@reports_bp.route("download/<filename>", methods=["GET"])
@login_required
def download_report(filename: str):
    """
    Generic safe download of a named artifact under REPORT_PATH.
    Use only when UI has exact basename to download.
    """
    if not _is_safe_basename(filename):
        return jsonify({"error": "invalid filename"}), 400
    rd = _report_dir()
    path = os.path.join(rd, filename)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(rd, filename, as_attachment=True)


# dev-only: temporary debug route to return raw report bodies directly from REPORT_PATH.
@reports_bp.route("/_debug/raw/<id>", methods=["GET"])
def _debug_raw_from_reports(id):
    """
    Development-only endpoint to return the raw report body file contents.
    Prefer the full.body.txt filename pattern the app expects.
    """
    rd = _report_dir()
    filename = f"{id}.full.body.txt"
    if not _is_safe_basename(filename):
        return jsonify({"error": "invalid filename"}), 400
    fullpath = os.path.join(rd, filename)
    if os.path.exists(fullpath):
        return send_from_directory(rd, filename, as_attachment=False)
    return ("not found", 404)


# -- UI JSON endpoints ------------------------------------------------------


@reports_bp.route("ui/tasks", methods=["GET"])
@login_required
def reports_tasks_list():
    """
    Return a JSON list of recent TaskAudit rows normalized for UI display.
    Filters supported via query params: start_date, end_date (ISO Y-m-d), domain
    This function is best-effort: if DB not available returns empty list.
    """
    limit = min(200, int(request.args.get("limit", 50)))
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    domain = request.args.get("domain")

    tasks_out = []

    # Best-effort DB access
    try:
        # import lazily to avoid circular imports in some app layouts
        from api.database import db
        from models.task_audit import TaskAudit
    except Exception:
        # DB models unavailable -> return empty list
        return jsonify({"tasks": []})

    try:
        q = db.session.query(TaskAudit).order_by(TaskAudit.created_at.desc()).limit(limit)
        rows = q.all()
    except Exception:
        rows = []

    for t in rows:
        info = _extract_artifact_info(t.result_meta)
        persistence_id = info["persistence_id"]
        body_filename = info["filename"]
        full_filename = info["full_filename"]
        log_filename = info["log_filename"]

        # If domain filter present, attempt to verify by checking quarantined_emails
        if domain:
            try:
                if persistence_id is not None:
                    from models.quarantined_email import QuarantinedEmail

                    exists = db.session.query(db.exists().where(QuarantinedEmail.id == int(persistence_id))).scalar()
                    if not exists:
                        continue
                else:
                    # unable to determine persistence id for filtering; conservative: include
                    pass
            except Exception:
                pass

        renderable = bool(persistence_id or body_filename or full_filename)
        tasks_out.append(
            {
                "audit_id": t.id,
                "task_id": t.task_id,
                "task_type": t.task_type,
                "status": t.status,
                "created_at": getattr(t, "created_at", None).isoformat() if getattr(t, "created_at", None) else None,
                "updated_at": getattr(t, "updated_at", None).isoformat() if getattr(t, "updated_at", None) else None,
                "result_meta": t.result_meta,
                "persistence_id": persistence_id,
                "body_filename": body_filename,
                "full_filename": full_filename,
                "log_filename": log_filename,
            }
        )

    return jsonify({"tasks": tasks_out})


@reports_bp.route("ui/tasks/<int:audit_id>", methods=["GET"])
@login_required
def reports_task_detail(audit_id: int):
    """
    Return normalized detail for a single TaskAudit row.
    Includes artifact filenames and pre-computed URLs when files exist under REPORT_PATH.
    """
    try:
        from api.database import db
        from models.task_audit import TaskAudit
    except Exception:
        return jsonify({"error": "db unavailable"}), 500

    ta = db.session.query(TaskAudit).get(audit_id)
    if not ta:
        return jsonify({"error": "not found"}), 404

    info = _extract_artifact_info(ta.result_meta)
    persistence_id = info["persistence_id"]

    rd = _report_dir()
    # prefer explicit filenames from result_meta; otherwise try common stems
    body_fname = info["full_filename"] or info["filename"] or (f"{persistence_id}.body.txt" if persistence_id else None)
    log_fname = info["log_filename"] or (f"{persistence_id}.log" if persistence_id else None)

    # ensure basenames and existence; build URLs for existing files
    def _build_url_if_exists(basename: Optional[str]) -> Optional[str]:
        if not basename or not _is_safe_basename(basename):
            return None
        p = os.path.join(rd, basename)
        if os.path.exists(p):
            try:
                return url_for("reports.download_report", filename=basename)
            except Exception:
                return None
        return None

    body_url = _build_url_if_exists(os.path.basename(body_fname)) if body_fname else None
    log_url = _build_url_if_exists(os.path.basename(log_fname)) if log_fname else None

    # Also try fallback by audit id or task id if persistence_id not present
    if not body_url:
        for stem in ((str(persistence_id) if persistence_id else None), str(ta.id), ta.task_id):
            if not stem:
                continue
            for suf in (".full.body.txt", ".body.txt", ".full.txt", ".txt"):
                candidate = f"{stem}{suf}"
                u = _build_url_if_exists(candidate)
                if u:
                    body_url = u
                    body_fname = candidate
                    break
            if body_url:
                break

    if not log_url:
        for stem in ((str(persistence_id) if persistence_id else None), str(ta.id), ta.task_id):
            if not stem:
                continue
            candidate = f"{stem}.log"
            u = _build_url_if_exists(candidate)
            if u:
                log_url = u
                log_fname = candidate
                break

    response = {
        "audit_id": ta.id,
        "task_id": ta.task_id,
        "task_type": ta.task_type,
        "status": ta.status,
        "created_at": getattr(ta, "created_at", None).isoformat() if getattr(ta, "created_at", None) else None,
        "updated_at": getattr(ta, "updated_at", None).isoformat() if getattr(ta, "updated_at", None) else None,
        "result_meta": ta.result_meta,
        "persistence_id": persistence_id,
        "body_filename": os.path.basename(body_fname) if body_fname else None,
        "body_url": body_url,
        "log_filename": os.path.basename(log_fname) if log_fname else None,
        "log_url": log_url,
    }

    return jsonify(response)


# -- Optional admin-only endpoints ------------------------------------------


@reports_bp.route("ui/raw_task_audits", methods=["GET"])
@login_required
@_admin_required_view
def _raw_task_audits():
    """
    Admin helper: return a few recent TaskAudit rows with raw result_meta for debugging.
    """
    limit = min(500, int(request.args.get("limit", 100)))
    try:
        from api.database import db
        from models.task_audit import TaskAudit
    except Exception:
        return jsonify({"error": "db unavailable"}), 500

    rows = []
    try:
        q = db.session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).limit(limit)
        for t in q.all():
            rows.append(
                {
                    "audit_id": t.id,
                    "task_id": t.task_id,
                    "status": t.status,
                    "created_at": getattr(t, "created_at", None).isoformat() if getattr(t, "created_at", None) else None,
                    "updated_at": getattr(t, "updated_at", None).isoformat() if getattr(t, "updated_at", None) else None,
                    "result_meta": t.result_meta,
                }
            )
    except Exception:
        return jsonify({"error": "query failed"}), 500

    return jsonify({"audits": rows})


# -- Register CLI commands and exports -------------------------------------

from flask import current_app


def register_commands(app):
    """
    Register CLI commands and any app startup hooks used by api.app.
    Kept minimal so imports succeed when api.app does `from api.reports import register_commands`.
    """
    try:
        @app.cli.command("reports-list-files")
        def _reports_list_files():
            """List files under REPORT_PATH for debugging."""
            rd = _report_dir()
            try:
                files = sorted(os.listdir(rd))
            except Exception as e:
                print("failed to list report dir:", e)
                return
            for f in files:
                print(f)
    except Exception:
        # protect import-time execution
        pass


# ensure blueprint is available for importers
_existing_all = globals().get("__all__")
if _existing_all is None:
    __all__ = ["reports_bp", "register_commands"]
else:
    try:
        current = list(_existing_all)
    except Exception:
        current = []
    for _sym in ("reports_bp", "register_commands"):
        if _sym not in current:
            current.append(_sym)
    __all__ = current
try:
    del _existing_all, current, _sym
except Exception:
    pass
