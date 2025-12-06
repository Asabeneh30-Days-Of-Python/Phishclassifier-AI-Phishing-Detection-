from flask import current_app
from api.database import db
app = current_app._get_current_object() if hasattr(current_app, "_get_current_object") else current_app
with app.app_context():
    from models.task_audit import TaskAudit
    rows = db.session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).limit(40).all()
    for r in rows:
        print("id=", r.id, "persistence_id=", getattr(r, "persistence_id", None), "status=", r.status)
        print(" result_meta:", getattr(r, "result_meta", None))
