# api/trainer.py
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
                                       # ↑ imported jsonify

from tasks import retrain_model_task  # Celery Task
from api.database import SessionLocal
from models.training_job import TrainingJob
from flask import jsonify                # for AJAX JSON responses
from sqlalchemy import desc
from sqlalchemy import text

trainer = Blueprint("trainer", __name__, url_prefix="/trainer")

DATASET_CHOICES = [
    {"id": "sample",     "label": "Sample Dataset"},
    {"id": "enron",      "label": "Enron Business Emails"},
    {"id": "spamassn",   "label": "SpamAssassin Public Corpus"},
    {"id": "phishtank",  "label": "PhishTank Confirmed Phishing"},
    {"id": "enterprise", "label": "Enterprise Logs"},
]

@trainer.route("/", methods=["GET", "POST"])
def train():
    if request.method == "POST":
        selected = request.form.getlist("datasets")
        if selected:
            # 1) Create + persist a pending job record with our own UUID
            job_id = str(uuid.uuid4())
            db     = SessionLocal()
            new_j  = TrainingJob(id=job_id,
                                 status="pending",
                                 datasets=selected)
            db.add(new_j)
            db.commit()
            db.close()

            # 2) Enqueue the Celery task with the same job_id
            retrain_model_task.apply_async(  # type: ignore[reportFunctionMemberAccess]
                args=[selected],
                task_id=job_id
            )

            # 3) Emit a Socket.IO update so front-end sees it immediately
            from api.app import socketio
            socketio.emit(
                "training_update",
                {
                    "id": job_id,
                    "status": "pending",
                    "datasets": selected
                },
                namespace="/trainer"
            )

        # If this POST came via AJAX, return JSON so client.json() succeeds
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"id": job_id, "datasets": selected}), 202  # type: ignore[reportPossiblyUnboundVariable] 

        # Otherwise do the normal redirect
        return redirect(url_for("trainer.train"))

    # GET: fetch the 10 most recent jobs
    db = SessionLocal()
    recent = (
        db.query(TrainingJob)
          .order_by(text("created_at DESC"))
          .limit(10)
          .all()
    )
    db.close()

    jobs = [
        {
            "id":       j.id,
            "status":   j.status,
            "datasets": j.datasets or []
        }
        for j in recent
    ]

    return render_template(
        "trainer.html",
        datasets=DATASET_CHOICES,
        jobs=jobs,
    )
