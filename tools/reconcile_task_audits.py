#!/usr/bin/env python3
"""
tools/reconcile_task_audits.py

One-time admin tool to help reconcile orphan TaskAudit rows with existing artifacts
under REPORT_PATH. Runs in dry-run mode by default. Use --apply to perform changes.

Usage (from container/app venv):
  python tools/reconcile_task_audits.py --dry-run
  python tools/reconcile_task_audits.py --apply

Safety:
 - Requires APP env or runs as a Flask CLI helper when imported under app context.
 - Changes are minimal: either populate TaskAudit.task_id when a matching artifact
   named <task_id>.* exists, or attach a normalized result_meta referencing a
   persistence id/artifact filename. Always prints actions before applying.
"""
import os
import argparse
import json

from datetime import datetime

# run under app context: import lazily
def main(dry_run=True, limit=200):
    try:
        from wsgi import app  # or your app factory
    except Exception:
        raise RuntimeError("Unable to import app; run this script inside project where wsgi.app is available")

    with app.app_context():
        from api.database import db
        from models.task_audit import TaskAudit

        report_dir = os.path.abspath(os.environ.get("REPORT_PATH", os.path.join(app.instance_path, "reports")))
        print("Report dir:", report_dir)
        print("Dry run:", dry_run)
        q = db.session.query(TaskAudit).order_by(TaskAudit.updated_at.desc()).limit(limit)
        candidates = []
        for ta in q.all():
            if ta.task_id:
                # already linked by task_id; skip
                continue
            info = {}
            try:
                rm = ta.result_meta or {}
                if isinstance(rm, str):
                    try:
                        rm = json.loads(rm)
                    except Exception:
                        rm = {}
                info = rm if isinstance(rm, dict) else {}
            except Exception:
                info = {}

            # Build potential stems to check
            stems = []
            # prefer persistence id if present
            pid = info.get("persistence_id") or info.get("id") or info.get("artifact_id")
            if pid:
                stems.append(str(pid))
            # audit id
            stems.append(str(ta.id))
            # try result_meta filenames
            fname = info.get("full_filename") or info.get("filename") or info.get("body_filename")
            if fname:
                stems.append(os.path.splitext(os.path.basename(str(fname)))[0])
            # check for any of these stems existing on disk (try common suffixes)
            suffixes = [".full.body.txt", ".body.txt", ".full.txt", ".txt", ".log"]
            found = None
            found_name = None
            for s in stems:
                if not s:
                    continue
                for suf in suffixes:
                    candidate = f"{s}{suf}"
                    p = os.path.join(report_dir, candidate)
                    if os.path.exists(p):
                        found = s
                        found_name = candidate
                        break
                if found:
                    break
            candidates.append((ta, found, found_name, info))

        # Report
        to_apply = []
        for ta, found, found_name, info in candidates:
            print("AUDIT:", ta.id, "task_id:", ta.task_id, "status:", ta.status)
            if found:
                print("  -> Found artifact:", found_name, "stem:", found)
                # Decision logic: if found stem looks like a uuid-like string (non-digit) prefer to populate task_id;
                # if found stem is numeric, prefer to set result_meta persistence_id and filenames.
                if str(found).isdigit():
                    # numeric persistence id -> set result_meta if missing or incomplete
                    desired_meta = {
                        "persistence_id": int(found),
                        "filename": f"{found}.body.txt",
                        "full_filename": f"{found}.full.body.txt",
                        "log_filename": f"{found}.log",
                    }
                    change = ("result_meta", desired_meta)
                else:
                    # non-numeric -> set task_id to the stem so UI can index by it
                    change = ("task_id", found)
                print("  Proposed change:", change)
                if not dry_run:
                    to_apply.append((ta, change))
            else:
                print("  -> No artifact found for audit", ta.id, " (result_meta:", info, ")")
        # apply if requested
        if not dry_run and to_apply:
            for ta, change in to_apply:
                kind, val = change
                print("Applying to audit", ta.id, ":", kind, val)
                if kind == "task_id":
                    ta.task_id = val
                elif kind == "result_meta":
                    try:
                        ta.result_meta = val
                    except Exception:
                        ta.result_meta = json.dumps(val, default=str)
                db.session.add(ta)
            db.session.commit()
            print("Applied", len(to_apply), "changes.")
        elif dry_run:
            print("Dry run complete. No changes applied.")
        else:
            print("No changes to apply.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the proposed changes (default is dry-run)")
    parser.add_argument("--limit", type=int, default=200, help="How many audits to examine")
    args = parser.parse_args()
    main(dry_run=not args.apply, limit=args.limit)
