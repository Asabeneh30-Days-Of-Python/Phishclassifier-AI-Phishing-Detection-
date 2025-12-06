# api/database.py
import os
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import logging

# Flask-SQLAlchemy entrypoint (extension instance)
db = SQLAlchemy()

# Pick up DATABASE_URL (fallback to instance SQLite file)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(os.path.dirname(__file__), "..", "instance", "phishclassifier.db"),
)

# SQL logger
sqla_logger = logging.getLogger("sqlalchemy.engine")

# Raw SQLAlchemy engine and session factory for non-Flask contexts (workers, CLI)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
SQL_ECHO = os.getenv("SQL_ECHO", "0").strip() == "1"
# enable pool_pre_ping to avoid stale connections on long-running processes
engine = create_engine(DATABASE_URL, connect_args=connect_args, echo=SQL_ECHO, pool_pre_ping=True)
# SessionLocal and exported SESSION_MAKER for worker/CLI compatibility
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
SESSION_MAKER = SessionLocal

# Declarative base (for any pure-SQLAlchemy models)
Base = declarative_base()


def enable_verbose_sql_logging(mask_columns=None):
    mask_columns = set(mask_columns or ["body"])

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        try:
            p = parameters
            if isinstance(p, dict):
                p_safe = {k: ("<masked>" if k in mask_columns else v) for k, v in p.items()}
            elif isinstance(p, (list, tuple)):
                p_safe = []
                for v in p:
                    if isinstance(v, dict):
                        p_safe.append({k: ("<masked>" if k in mask_columns else v2) for k, v2 in v.items()})
                    else:
                        p_safe.append(v if (isinstance(v, (int, float, type(None))) or (isinstance(v, str) and len(v) < 200)) else f"{v[:120]}...")
            else:
                p_safe = repr(p)
        except Exception:
            p_safe = "<params-serialize-failed>"
        sqla_logger.debug("SQL: %s -- params: %s", statement, p_safe)

    try:
        from sqlalchemy import event

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
    except Exception:
        sqla_logger.exception("Failed to attach SQL logging hook")


def init_db(app=None) -> None:
    """
    Create tables for:
      - Flask-SQLAlchemy models via db.create_all() when AUTO_CREATE_DB=1 and app provided
      - Any raw-SQLAlchemy Base metadata via Base.metadata.create_all(bind=engine)

    Ensures instance directory exists. Imports models.task_audit so models register.
    """
    try:
        if app is not None and hasattr(app, "instance_path"):
            instance_dir = app.instance_path
        else:
            instance_dir = os.path.join(os.path.dirname(__file__), "..", "instance")
        os.makedirs(instance_dir, exist_ok=True)
    except Exception:
        pass

    # Import root-level models so Flask-SQLAlchemy sees them when create_all runs.
    try:
        import models.task_audit  # noqa: F401
    except Exception:
        pass

    # Only auto-create Flask-SQLAlchemy tables when explicitly allowed (dev)
    if app is not None and os.environ.get("AUTO_CREATE_DB", "0").strip() == "1":
        with app.app_context():
            db.create_all()

    # Always ensure raw-SQLAlchemy Base metadata is created on the engine (best-effort)
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        pass


def get_engine():
    """
    Return the underlying SQLAlchemy engine used by SessionLocal.
    Useful for raw connections, health checks, or CLI tooling.
    """
    return engine
