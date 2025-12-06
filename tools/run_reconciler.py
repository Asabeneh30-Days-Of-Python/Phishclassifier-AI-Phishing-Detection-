"""
tools/run_reconciler.py

Wrapper that boots the Flask app, registers the application's SQLAlchemy
extension instance, enters an application context and runs the reconciler
module.

Behavior:
- Respects DRY_RUN environment variable (1/true to preview, 0/false to apply).
- Initializes logging to stdout.
- Calls the reconciler's public entrypoint (tries main(), then run()).
- Catches and logs exceptions, returns non-zero exit code on failure.
"""

import importlib
import logging
import os
import sys
from typing import Callable

# Adjust these imports to match your project layout if needed.
# - create_app: app factory that returns a Flask instance
# - db: the SQLAlchemy() extension object used by the app
from api import create_app
from api.database import db

LOG = logging.getLogger("run_reconciler")


def setup_logging():
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def find_entry_callable(module, names=("main", "run")) -> Callable | None:
    for name in names:
        if hasattr(module, name) and callable(getattr(module, name)):
            return getattr(module, name)
    return None


def run_reconciler(dry_run: bool) -> int:
    """
    Boot the app, register db, enter app context, run reconciler module.
    Returns exit code 0 on success, non-zero on failure.
    """
    LOG.info("run_reconciler starting: dry_run=%s", dry_run)

    # Create app
    try:
        app = create_app()
    except Exception as exc:
        LOG.exception("Failed to create app via create_app(): %s", exc)
        return 2

    # Ensure the shared SQLAlchemy instance is initialized with the app.
    try:
        db.init_app(app)
    except Exception as exc:
        LOG.exception("Failed to init_app on SQLAlchemy instance: %s", exc)
        return 3

    # Import reconciler module and discover entrypoint
    try:
        reconciler_mod = importlib.import_module("tools.reconciler_backfill")
    except Exception as exc:
        LOG.exception("Failed to import tools.reconciler_backfill: %s", exc)
        return 4

    entry = find_entry_callable(reconciler_mod)
    if not entry:
        LOG.error("No entry callable (main/run) found in tools.reconciler_backfill")
        return 5

    # Propagate DRY_RUN to reconciler if it expects env var
    if dry_run:
        os.environ.setdefault("DRY_RUN", "1")
    else:
        os.environ.setdefault("DRY_RUN", "0")

    # Run the reconciler inside the app context
    try:
        with app.app_context():
            LOG.info("Running reconciler entrypoint %s inside app context", entry.__name__)
            # If reconciler entry returns a value, propagate it as exit code.
            result = entry()
            if isinstance(result, int) and result != 0:
                LOG.error("Reconciler returned non-zero exit code: %s", result)
                return result
    except Exception:
        LOG.exception("Uncaught exception while running reconciler")
        return 6

    LOG.info("run_reconciler finished successfully")
    return 0


def main():
    setup_logging()
    dry_env = os.environ.get("DRY_RUN", "").lower()
    dry_run = dry_env in ("1", "true", "yes")
    exit_code = run_reconciler(dry_run=dry_run)
    # If this is executed as a module, exit with appropriate status to surface failures.
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
