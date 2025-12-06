"""
Reports UI endpoints and JSON helpers for the Reports feature.

Provides:
- HTML dashboard: GET /reports/ui/
- JSON list of reports: GET  /reports/ui/list
- Enqueue/generate reports: POST /reports/ui/generate
- Task status lookup: GET /reports/ui/task_status/<task_id>
- Task list and task detail used by frontend:
  - GET  /reports/ui/tasks
  - GET  /reports/ui/tasks/<task_id>
  - GET  /reports/ui/tasks/<task_id>/logs
  - POST /reports/ui/tasks/<task_id>/cancel
- Delete report file: POST /reports/ui/delete
- Download redirect helper: GET /reports/ui/download/<filename>
"""
from datetime import datetime
import json
import os
import time
from typing import Optional, Dict, Any

from flask import (
    Blueprint,
    render_template,
    request,
    jsonify,
    current_app,
    url_for,
    redirect,
)
from flask_login import login_required
from decorators import admin_required
from werkzeug.utils import secure_filename

from api.reports import _report_dir, _generate_and_write_report

try:
    from celery.result import AsyncResult  # type: ignore
except Exception:
    AsyncResult = None  # type: ignore

try:
    from models.task_audit import TaskAudit  # type: ignore
except Exception:
    TaskAudit = None  # type: ignore

reports_ui = Blueprint("reports_ui", __name__, url_prefix="/reports/ui")


@reports_ui.route("/", methods=["GET"])
@login_required
@admin_required
def reports_dashboard():
    report_dir = _report_dir()
    return render_template("reports.html", report_dir=report_dir)


@reports_ui.route("list", methods=["GET"])
@login_required
@admin_required
def reports_list():
    report_dir = _report_dir()
    try:
        files = []
        for f in sorted(os.listdir(report_dir), reverse=True):
            p = os.path.join(report_dir, f)
            if os.path.isfile(p):
                files.append(
                    {
                        "name": f,
                        "size": os.path.getsize(p),
                        "mtime": os.path.getmtime(p),
                        "download_url": url_for("reports.download_report", filename=f),
                    }
                )
        current_app.logger.info(
            "reports_ui: listed %d files user=%s", len(files), getattr(request, "remote_addr", "-")
        )
        return jsonify({"reports": files}), 200
    except Exception:
        current_app.logger.exception("reports_ui: failed to list reports")
        return jsonify({"error": "list failed"}), 500


def _filters_to_csv_text(flt: Dict[str, Any]) -> str:
    """
    Best-effort CSV generator for filters. If DB readable, it will query matching quarantined_emails.
    Fallback: serialize filters as text.
    """
    try:
        from api.database import db as _db
        from models.quarantined_email import QuarantinedEmail

        q = QuarantinedEmail.query
        if flt.get("start_date"):
            try:
                from datetime import datetime as _dt
                sd = _dt.fromisoformat(flt.get("start_date"))
                q = q.filter(QuarantinedEmail.received_at >= sd)
            except Exception:
                pass
        if flt.get("end_date"):
            try:
                from datetime import datetime as _dt, timedelta as _td
                ed = _dt.fromisoformat(flt.get("end_date"))
                end_inclusive = ed + _td(days=1) - _td(microseconds=1)
                q = q.filter(QuarantinedEmail.received_at <= end_inclusive)
            except Exception:
                pass
        if flt.get("domain"):
            q = q.filter(QuarantinedEmail.sender.ilike(f"%{flt.get('domain')}%"))
        rows = q.order_by(QuarantinedEmail.received_at.desc()).limit(1000).all()
        import io, csv

        out = io.StringIO()
        writer = csv.writer(out)
        headers = ["id", "sender", "subject", "received_at", "risk_score", "feedback"]
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r.id, (r.sender or ""), (r.subject or "")[:200], (r.received_at.isoformat() if getattr(r, "received_at", None) else ""), r.risk_score, r.feedback])
        return out.getvalue()
    except Exception:
        return "filters: " + json.dumps(flt)


@reports_ui.route("/generate", methods=["POST"])
@login_required
@admin_required
def reports_generate():
    payload = request.get_json(silent=True) or {}
    filters: Dict[str, Any] = {}
    for k in ("start_date", "end_date", "min_score", "max_score", "domain"):
        if payload.get(k) is not None:
            filters[k] = payload.get(k)

    audit_id = None
    audit_row_id = None
    if TaskAudit is not None:
        try:
            from api.database import db as _db  # type: ignore

            sess = _db.session
            ta = TaskAudit(
                task_type="report",
                task_id=None,
                user=getattr(request, "remote_addr", "-"),
                payload=filters or {},
                status="pending",
                created_at=datetime.utcnow(),
            )
            sess.add(ta)
            try:
                sess.commit()
            except Exception:
                try:
                    sess.flush()
                except Exception:
                    pass
            try:
                sess.refresh(ta)
            except Exception:
                pass
            audit_id = getattr(ta, "id", None) or getattr(ta, "task_id", None)
            audit_row_id = getattr(ta, "id", None)
        except Exception:
            current_app.logger.debug("reports_ui: optimistic TaskAudit creation skipped")

    try:
        from celery import current_app as celery_current_app  # type: ignore

        task = celery_current_app.send_task("reports.generate_report_task", args=[filters], kwargs={"audit_id": audit_id})
        task_id = getattr(task, "id", None)
        current_app.logger.info(
            "reports_ui: enqueued report generate task=%s user=%s filters=%s audit_id=%s",
            task_id,
            getattr(request, "remote_addr", "-"),
            filters,
            audit_id,
        )
        if audit_row_id is not None:
            try:
                from api.database import db as _db  # type: ignore

                sess = _db.session
                ta = sess.query(TaskAudit).filter_by(id=audit_row_id).first()
                if ta:
                    ta.task_id = task_id
                    sess.add(ta)
                    try:
                        sess.commit()
                    except Exception:
                        try:
                            sess.flush()
                        except Exception:
                            pass
            except Exception:
                current_app.logger.exception("reports_ui: failed to attach task_id to TaskAudit (non-fatal)")
        try:
            from api.app import socketio as _socketio  # type: ignore

            payload_out = {
                "task_id": task_id,
                "task_type": "report",
                "summary": "quarantine report",
                "user": getattr(request, "remote_addr", "-"),
                "audit_id": audit_id,
                "created_at": datetime.utcnow().isoformat(),
                "status": "pending",
            }
            _socketio.emit("task:created", payload_out, namespace="/reports")
        except Exception:
            current_app.logger.debug("reports_ui: socket emit task:created failed (non-fatal)")
        return jsonify({"status": "queued", "task_id": task_id, "audit_id": audit_id}), 202
    except Exception:
        try:
            current_app.logger.info(
                "reports_ui: generating synchronously (celery unavailable) user=%s filters=%s",
                getattr(request, "remote_addr", "-"),
                filters,
            )

            # Build content and write atomically using audit_row_id or timestamp as stem
            content = _filters_to_csv_text(filters)
            stem = str(audit_row_id) if audit_row_id else f"report-{int(time.time())}"
            path = _generate_and_write_report(stem, content, suffix=".csv")
            filename = os.path.basename(path) if path else None
            try:
                from api.app import socketio as _socketio  # type: ignore

                payload_out = {
                    "task_id": None,
                    "task_type": "report",
                    "summary": "quarantine report",
                    "user": getattr(request, "remote_addr", "-"),
                    "audit_id": audit_id,
                    "created_at": datetime.utcnow().isoformat(),
                    "status": "succeeded",
                    "result_meta": {"filename": filename} if filename else None,
                }
                _socketio.emit("task:updated", payload_out, namespace="/reports")
            except Exception:
                current_app.logger.debug("reports_ui: sync emit failed")
            return jsonify({"status": "done", "filename": filename}), 200
        except Exception:
            current_app.logger.exception("reports_ui: generating synchronously failed")
            return jsonify({"error": "generate failed"}), 500


@reports_ui.route("/task_status/<task_id>", methods=["GET"])
@login_required
@admin_required
def task_status(task_id):
    try:
        if TaskAudit is None:
            try:
                if AsyncResult is not None:
                    ar = AsyncResult(task_id)
                    return jsonify({"task_id": task_id, "state": ar.state}), 200
            except Exception:
                pass
            return jsonify({"task_id": task_id, "state": "unknown"}), 200
        ta = TaskAudit.query.filter((TaskAudit.task_id == task_id) | (TaskAudit.id == task_id)).order_by(TaskAudit.updated_at.desc()).first()
        if not ta:
            return jsonify({"task_id": task_id, "state": "not_found"}), 404
        res_meta = ta.result_meta
        if isinstance(res_meta, str):
            try:
                res_meta = json.loads(res_meta)
            except Exception:
                res_meta = res_meta
        return jsonify({"task_id": task_id, "state": ta.status, "result_meta": res_meta}), 200
    except Exception:
        current_app.logger.exception("reports_ui: task_status failed")
        return jsonify({"error": "task status failed"}), 500


@reports_ui.route("/delete", methods=["POST"])
@login_required
@admin_required
def reports_delete():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "missing name"}), 400
    if ".." in name or name.startswith("/"):
        return jsonify({"error": "invalid name"}), 400
    report_dir = _report_dir()
    path = os.path.join(report_dir, secure_filename(name))
    try:
        if os.path.exists(path) and os.path.isfile(path):
            os.remove(path)
            current_app.logger.info("reports_ui: deleted report=%s user=%s", name, getattr(request, "remote_addr", "-"))
            return jsonify({"status": "ok"}), 200
        else:
            return jsonify({"error": "not found"}), 404
    except Exception:
        current_app.logger.exception("reports_ui: failed to delete %s", name)
        return jsonify({"error": "delete failed"}), 500


@reports_ui.route("/download/<path:filename>", methods=["GET"])
@login_required
@admin_required
def reports_download(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400
    try:
        return redirect(url_for("reports.download_report", filename=filename))
    except Exception:
        current_app.logger.exception("reports_ui: failed to redirect download for %s", filename)
        return jsonify({"error": "download failed"}), 500


@reports_ui.route("/tasks", methods=["GET"])
@login_required
@admin_required
def reports_tasks_list():
    try:
        if TaskAudit is None:
            return jsonify({"tasks": []}), 200
        limit = min(200, int(request.args.get("limit", 50)))
        offset = int(request.args.get("offset", 0))
        task_type = request.args.get("task_type")
        q = TaskAudit.query
        if task_type:
            q = q.filter_by(task_type=task_type)
        q = q.order_by(TaskAudit.updated_at.desc()).limit(limit).offset(offset)
        tasks = []
        for t in q:
            rm = t.result_meta
            persistence_id = None
            body_filename = None
            full_filename = None
            log_filename = None
            try:
                if isinstance(rm, str):
                    try:
                        parsed = json.loads(rm)
                        if isinstance(parsed, dict):
                            rm = parsed
                    except Exception:
                        pass
                if isinstance(rm, dict):
                    persistence_id = rm.get("persistence_id") or rm.get("id")
                    body_filename = rm.get("filename") or rm.get("body_filename")
                    full_filename = rm.get("full_filename")
                    log_filename = rm.get("log_filename")
            except Exception:
                pass
            tasks.append(
                {
                    "audit_id": t.id,
                    "task_id": t.task_id,
                    "task_type": t.task_type,
                    "user": t.user,
                    "status": t.status,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                    "payload": t.payload,
                    "result_meta": rm,
                    "summary": (t.payload.get("summary") if isinstance(t.payload, dict) else None),
                    "persistence_id": persistence_id,
                    "body_filename": body_filename,
                    "full_filename": full_filename,
                    "log_filename": log_filename,
                }
            )
        return jsonify({"tasks": tasks}), 200
    except Exception:
        current_app.logger.exception("reports_ui: tasks list failed")
        return jsonify({"error": "tasks list failed"}), 500


@reports_ui.route("/tasks/<task_id>", methods=["GET"])
@login_required
@admin_required
def reports_task_detail(task_id):
    try:
        if TaskAudit is None:
            return jsonify({"error": "not found"}), 404
        from models.task_audit import TaskLog  # noqa: E402

        ta = (
            TaskAudit.query.filter((TaskAudit.task_id == task_id) | (TaskAudit.id == task_id))
            .order_by(TaskAudit.updated_at.desc())
            .first()
        )
        if not ta:
            return jsonify({"error": "not found"}), 404

        last_logs = []
        try:
            logs = TaskLog.query.filter_by(task_audit_id=ta.id).order_by(TaskLog.ts.desc()).limit(200).all()
            last_logs = [{"ts": l.ts.isoformat() if l.ts else None, "level": l.level, "message": l.message} for l in reversed(logs)]
        except Exception:
            current_app.logger.exception("reports_ui: fetch last_logs failed")

        body_exists = False
        body_filename = None
        body_url = None
        log_filename = None
        log_url = None
        try:
            report_dir = _report_dir()
            candidates = []
            if ta.task_id:
                candidates.append(f"{ta.task_id}.body.txt")
                candidates.append(f"{ta.task_id}.full.body.txt")
            if ta.id:
                candidates.append(f"{ta.id}.body.txt")
                candidates.append(f"{ta.id}.full.body.txt")
            fn = None
            try:
                if isinstance(ta.result_meta, str):
                    try:
                        parsed = json.loads(ta.result_meta)
                        if isinstance(parsed, dict):
                            fn = parsed.get("body_filename") or parsed.get("filename") or parsed.get("body_path")
                    except Exception:
                        fn = None
                elif isinstance(ta.result_meta, dict):
                    fn = ta.result_meta.get("body_filename") or ta.result_meta.get("filename") or ta.result_meta.get("body_path")
            except Exception:
                fn = None
            if fn:
                candidates.append(os.path.basename(str(fn)))
            for c in candidates:
                if not c:
                    continue
                p = os.path.join(report_dir, c)
                if os.path.exists(p) and os.path.isfile(p):
                    body_exists = True
                    body_filename = os.path.basename(c)
                    try:
                        base = os.path.splitext(body_filename)[0]
                        body_url = url_for("reports.body_report", task_id=base)
                    except Exception:
                        try:
                            body_url = url_for("reports.download_report", filename=body_filename)
                        except Exception:
                            body_url = None
                    break
            candidate_logs = []
            if ta.id:
                candidate_logs.append(f"{ta.id}.log")
            if ta.task_id:
                candidate_logs.append(f"{ta.task_id}.log")
            if fn:
                candidate_logs.append(f"{os.path.splitext(os.path.basename(str(fn)))[0]}.log")
            for c in candidate_logs:
                p = os.path.join(report_dir, c)
                if os.path.exists(p) and os.path.isfile(p):
                    log_filename = os.path.basename(c)
                    try:
                        log_url = url_for("reports.download_report", filename=log_filename)
                    except Exception:
                        log_url = None
                    break
        except Exception:
            current_app.logger.debug("reports_ui: body/log existence check failed for task %s", task_id)

        res_meta = ta.result_meta
        if isinstance(res_meta, str):
            try:
                res_meta = json.loads(res_meta)
            except Exception:
                res_meta = res_meta

        return (
            jsonify(
                {
                    "audit_id": ta.id,
                    "task_id": ta.task_id,
                    "task_type": ta.task_type,
                    "payload": ta.payload,
                    "status": ta.status,
                    "created_at": ta.created_at.isoformat() if ta.created_at else None,
                    "started_at": ta.started_at.isoformat() if ta.started_at else None,
                    "finished_at": ta.finished_at.isoformat() if ta.finished_at else None,
                    "result_meta": res_meta,
                    "last_logs": last_logs,
                    "trace_id": (ta.result_meta.get("trace_id") if isinstance(ta.result_meta, dict) else None),
                    "body_exists": body_exists,
                    "body_filename": body_filename,
                    "body_url": body_url,
                    "log_filename": log_filename,
                    "log_url": log_url,
                }
            ),
            200,
        )
    except Exception:
        current_app.logger.exception("reports_ui: task detail failed")
        return jsonify({"error": "task detail failed"}), 500


@reports_ui.route("/tasks/<task_id>/logs", methods=["GET"])
@login_required
@admin_required
def reports_task_logs(task_id):
    try:
        if TaskAudit is None:
            return jsonify({"error": "not found"}), 404
        from models.task_audit import TaskLog  # noqa: E402

        audit = TaskAudit.query.filter((TaskAudit.task_id == task_id) | (TaskAudit.id == task_id)).first()
        if not audit:
            return jsonify({"error": "not found"}), 404
        limit = min(1000, int(request.args.get("limit", 200)))
        offset = int(request.args.get("offset", 0))
        tail = request.args.get("tail", "0") == "1"
        if tail:
            q = TaskLog.query.filter_by(task_audit_id=audit.id).order_by(TaskLog.ts.desc())
            if offset:
                q = q.offset(offset)
            logs = q.limit(limit).all()
            out = [{"ts": l.ts.isoformat() if l.ts else None, "level": l.level, "message": l.message, "meta": l.meta} for l in reversed(logs)]
        else:
            q = TaskLog.query.filter_by(task_audit_id=audit.id).order_by(TaskLog.ts.asc())
            if offset:
                q = q.offset(offset)
            logs = q.limit(limit).all()
            out = [{"ts": l.ts.isoformat() if l.ts else None, "level": l.level, "message": l.message, "meta": l.meta} for l in logs]
        return jsonify({"logs": out}), 200
    except Exception:
        current_app.logger.exception("reports_ui: task logs failed")
        return jsonify({"error": "logs failed"}), 500


@reports_ui.route("/tasks/<task_id>/cancel", methods=["POST"])
@login_required
@admin_required
def reports_task_cancel(task_id):
    try:
        from celery import current_app as celery_current_app  # type: ignore
        try:
            celery_current_app.control.revoke(task_id, terminate=True)
        except Exception:
            try:
                if AsyncResult is not None:
                    AsyncResult(task_id).revoke(terminate=True)
            except Exception:
                pass
        try:
            if TaskAudit is not None:
                try:
                    from tasks import SESSION_MAKER as _SESSION_MAKER  # type: ignore
                except Exception:
                    _SESSION_MAKER = None
                db_session = None
                try:
                    if _SESSION_MAKER:
                        db_session = _SESSION_MAKER() if callable(_SESSION_MAKER) else _SESSION_MAKER
                except Exception:
                    db_session = None
                if db_session:
                    try:
                        ta = db_session.query(TaskAudit).filter_by(task_id=task_id).first()
                        if ta:
                            ta.status = "cancelled"
                            db_session.add(ta)
                            try:
                                db_session.commit()
                            except Exception:
                                try:
                                    db_session.flush()
                                except Exception:
                                    pass
                    finally:
                        try:
                            close_fn = getattr(db_session, "close", None)
                            if callable(close_fn):
                                close_fn()
                        except Exception:
                            pass
        except Exception:
            current_app.logger.exception("reports_ui: failed to mark audit cancelled")
        try:
            from api.app import socketio as _socketio  # type: ignore
            _socketio.emit(
                "task:updated",
                {"task_id": task_id, "status": "cancelled", "updated_at": datetime.utcnow().isoformat()},
                namespace="/reports",
            )
        except Exception:
            pass
        return jsonify({"status": "cancelled"}), 200
    except Exception:
        current_app.logger.exception("reports_ui: cancel failed")
        return jsonify({"error": "cancel failed"}), 500
