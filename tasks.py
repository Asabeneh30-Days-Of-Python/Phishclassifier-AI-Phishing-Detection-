# tasks.py

import os
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from celery import Celery
from kombu import Exchange, Queue

from api.database import SessionLocal
from models.training_job import TrainingJob
from models.quarantined_email import QuarantinedEmail
from train_model import train_and_save_model

# Broker & result backend URLs (Redis)
CELERY_BROKER_URL     = os.getenv("CELERY_BROKER_URL",     "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

# Initialize Celery app
celery = Celery(
    "tasks",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND
)

# Configure queues, result expiry, and periodic tasks (via beat_schedule)
celery.conf.update(
    task_default_queue="cpu",
    task_default_exchange="cpu",
    task_default_routing_key="cpu",
    task_queues=(
        Queue("cpu", Exchange("cpu", type="direct"), routing_key="cpu"),
    ),
    result_expires=3600,  # expire task results after one hour

    # Schedule purge_old every 24h
    beat_schedule={
        "purge_old_quarantined": {
            "task": "tasks.purge_old",
            "schedule": 86400.0,
        },
    },
)

# Basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(message)s"
)


@celery.task
def purge_old() -> None:
    """
    Delete QuarantinedEmail records older than 30 days.
    """
    db     = SessionLocal()
    cutoff = datetime.utcnow() - timedelta(days=30)
    deleted = (
        db.query(QuarantinedEmail)
          .filter(QuarantinedEmail.created_at < cutoff)
          .delete(synchronize_session=False)
    )
    db.commit()
    db.close()
    logging.info("purge_old: deleted %d rows older than %s", deleted, cutoff)


@celery.task(bind=True, queue="cpu")
def retrain_model_task(self, datasets: List[str]) -> None:
    """
    Retrain the phishing model, update DB status, and broadcast via Socket.IO.
    """
    job_id = self.request.id
    db     = SessionLocal()
    job    = db.query(TrainingJob).get(job_id)
    if job is None:
        logging.error("retrain_model_task: job %s not found", job_id)
        db.close()
        return

    from api.app import socketio

    job.status = "running"
    db.commit()
    socketio.emit(
        "training_update",
        {"id": job_id, "status": "running", "datasets": job.datasets},
        namespace="/trainer"
    )

    try:
        start_time = time.time()
        train_and_save_model(datasets)
        elapsed = time.time() - start_time
        logging.info("TRAIN_FINISHED for job %s in %.1fs", job_id, elapsed)

        job.status = "completed"
        db.commit()
        socketio.emit(
            "training_update",
            {"id": job_id, "status": "completed", "datasets": job.datasets},
            namespace="/trainer"
        )
    except Exception:
        job.status = "failed"
        db.commit()
        socketio.emit(
            "training_update",
            {"id": job_id, "status": "failed", "datasets": job.datasets},
            namespace="/trainer"
        )
        raise
    finally:
        db.close()


@celery.task(bind=True, queue="cpu", name="classify_email_task")
def classify_email_task(
    self,
    email_id: str,
    content: str
) -> Dict[str, Any]:
    """
    Asynchronously classify an email and return a structured result dict.
    Optionally persists to QuarantinedEmail if email_id is passed.
    """
    logging.info("Starting classify_email_task for email_id=%s", email_id)

    # Import your model’s classify function
    try:
        from api.classifier import classify_text  # type: ignore
    except ImportError:
        logging.error("Cannot import classify_text")
        raise

    # Expect classify_text to return a dict like:
    # {
    #   "prediction": str,
    #   "risk_score": float,
    #   "technical_explanation": [...],
    #   "attacker_insights": {
    #       "motives": [...],
    #       "recommendations": [...],
    #       "detailed_reasoning": [...]
    #   }
    # }
    result = classify_text(content)

    if email_id:
        db     = SessionLocal()
        record = db.query(QuarantinedEmail).get(email_id)
        if record:
            record.prediction = result.get("prediction")
            record.risk_score = result.get("risk_score")
            db.commit()
        db.close()

    logging.info("Completed classify_email_task: %s", result)
    return result
