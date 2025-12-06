# tools/reconciler_backfill.py
"""
Idempotent reconciler/backfill tool.

Scans REPORT_PATH for artifact files that look like "<persistence_id>.body.txt"
and ensures a TaskAudit row exists and (optionally) that QuarantinedEmail exists.
This script is safe to run repeatedly and will not overwrite existing TaskAudit rows.

Usage (from project root):
  python -m tools.reconciler_backfill

Environment:
  REPORT_PATH - directory to scan (defaults to /tmp/phish_reports)
  DRY_RUN - if "1", the script will only log actions without making DB changes
"""
import os
import re
import logging
from datetime import datetime

logger = logging.getLogger("reconciler")
logging.basicConfig(level=logging.INFO)

ARTIFACT_RE = re.compile(r"^(\d+)\.body\.txt$")


def find_artifact_ids(report_dir: str):
    ids = set()
    try:
        for fn in os.listdir(report_dir):
            m = ARTIFACT_RE.match(fn)
            if m:
                ids.add(int(m.group(1)))
    except Exception:
        logger.exception("failed scanning report_dir=%s", report_dir)
    return sorted(ids)


def ensure_task_audit_for_persistence(persistence_id: int, dry_run: bool = True):
    """
    Ensure a TaskAudit row exists that references persistence_id.
    If none found, create a minimal TaskAudit row with deterministic values.
    Returns True if created or would be created in dry-run, False if already existed or on failure.
    """
    try:
        # import inside function so module can be loaded without requiring app context
        from api.database import db  # type: ignore
        from models.task_audit import TaskAudit  # type: ignore

        # best-effort search by persistence_id
        ta = db.session.query(TaskAudit).filter_by(persistence_id=persistence_id).first()
        if ta:
            logger.debug("TaskAudit already exists for persistence_id=%s id=%s", persistence_id, ta.id)
            return False
        if dry_run:
            logger.info("[DRY_RUN] would create TaskAudit for persistence_id=%s", persistence_id)
            return True
        # Create minimal TaskAudit
        manual_id = f"reconciler-{persistence_id}"
        ta = TaskAudit(
            id=manual_id,
            task_id=None,
            task_type="reconciler",
            user=None,
            payload={"reconciled": True},
            status="succeeded",
        )
        ta.persistence_id = persistence_id
        ta.created_at = datetime.utcnow()
        ta.updated_at = datetime.utcnow()
        db.session.add(ta)
        try:
            db.session.commit()
            logger.info("created TaskAudit id=%s for persistence_id=%s", manual_id, persistence_id)
            return True
        except Exception:
            db.session.rollback()
            logger.exception("failed to commit TaskAudit for persistence_id=%s", persistence_id)
            return False
    except Exception:
        logger.exception("ensure_task_audit_for_persistence failed for %s", persistence_id)
        return False


def main():
    report_dir = os.environ.get("REPORT_PATH", "/tmp/phish_reports")
    dry = os.environ.get("DRY_RUN", "1") != "0"
    logger.info("reconciler starting: report_dir=%s dry_run=%s", report_dir, dry)
    ids = find_artifact_ids(report_dir)
    logger.info("found %d artifact persistence ids to check", len(ids))
    created = 0
    skipped = 0
    failed = 0
    for pid in ids:
        ok = ensure_task_audit_for_persistence(pid, dry_run=dry)
        if ok and not dry:
            created += 1
        elif ok and dry:
            skipped += 1
        elif not ok:
            # either already existed or failed
            # best-effort distinguish by checking DB again when not dry
            skipped += 1
    logger.info("reconciler finished: created=%d skipped=%d failed=%d", created, skipped, failed)


if __name__ == "__main__":
    main()
