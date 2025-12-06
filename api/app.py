# api/app.py
import os
import sys
import json
import re
from urllib.parse import urlparse, urljoin
from functools import wraps
from datetime import datetime, timedelta
from typing import Optional, Any, Callable, List

# Ensure the project root is on PYTHONPATH so tasks.py at root can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flask import (
    Flask,
    render_template,
    request,
    abort,
    jsonify,
    redirect,
    url_for,
    flash,
    current_app,
    Response,
)
from flask_cors import CORS
from flask_socketio import SocketIO
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from flasgger import Swagger
from sqlalchemy import desc, text as sa_text, text
from werkzeug.security import generate_password_hash

# Forms
from forms import EmailForm, LoginForm, RegistrationForm

# Models
from models.user import User, Role
from models.feedback import Feedback
from models.quarantined_email import QuarantinedEmail, FeedbackAudit

# Extensions & Blueprints
from api.database import db, init_db
from api.logger import setup_logging
from api.reports import reports_bp, register_commands
from api.notifications import notifications_bp
from api.trainer import trainer as trainer_bp
from api.routes import prediction as api_blueprint
from api.reports_ui import reports_ui

# Observability: try Prometheus client and provide safe fallbacks
try:
    from prometheus_flask_exporter import PrometheusMetrics
except ImportError:
    PrometheusMetrics = None

try:
    from prometheus_client import Counter as _Counter, Histogram as _Histogram
    from prometheus_client import generate_latest as _generate_latest, CONTENT_TYPE_LATEST as _CONTENT_TYPE_LATEST

    PROM_CLIENT_AVAILABLE = True
    Counter: Any = _Counter
    Histogram: Any = _Histogram
    generate_latest: Callable[..., bytes] = _generate_latest
    CONTENT_TYPE_LATEST: str = _CONTENT_TYPE_LATEST
except Exception:
    PROM_CLIENT_AVAILABLE = False

    def generate_latest() -> bytes:
        return b""

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class Counter:
        def __init__(self, *a, **k): pass
        def inc(self, *a, **k): pass

    class Histogram:
        def __init__(self, *a, **k): pass
        def observe(self, *a, **k): pass

# Module-level Prometheus metrics (no-op if client missing)
email_counter = Counter("emails_processed", "Total emails")
risk_hist = Histogram("risk_score", "Risk score distribution")

try:
    import ddtrace
    from ddtrace import tracer, patch_all
    patch_all(eventlet=False)
except ImportError:
    tracer = None

try:
    import bleach
except ImportError:
    bleach = None

# Authentication
login_manager = LoginManager()
login_manager.login_view = "login"

# SOCKET.IO INSTANCE
# Create the SocketIO object at module level but initialize it with app after create_app
socketio = SocketIO(async_mode="eventlet", logger=True, engineio_logger=True)

def is_safe_url(target: str) -> bool:
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return (
        test_url.scheme in ("http", "https")
        and ref_url.netloc == test_url.netloc
    )


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return f(*args, **kwargs)
    return decorated


@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None


def conditional_login_required(func):
    """
    Apply login_required only when ALLOW_ANONYMOUS_CLASSIFY is not set.
    """
    allow_anonymous = os.environ.get("ALLOW_ANONYMOUS_CLASSIFY", "0").strip() == "1"
    if allow_anonymous:
        return func
    return login_required(func)


# --- Safe NLTK helpers (fallbacks) ---
def safe_word_tokenize(text: str) -> List[str]:
    try:
        import nltk
        from nltk.tokenize import word_tokenize
        return word_tokenize(text or "")
    except Exception:
        return re.findall(r"\w+(?:['`-]\w+)?|[^\s\w]", (text or ""))

def safe_sent_tokenize(text: str) -> List[str]:
    try:
        import nltk
        from nltk.tokenize import sent_tokenize
        return sent_tokenize(text or "")
    except Exception:
        s = (text or "").strip()
        if not s:
            return []
        parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+', s) if p.strip()]
        return parts if parts else [s]

def _ensure_nltk_punkt(logger: Any) -> None:
    try:
        import nltk
        try:
            nltk.data.find("tokenizers/punkt")
            logger.info("NLTK punkt tokenizer present")
            return
        except LookupError:
            logger.info("NLTK punkt missing; attempting download (non-fatal)")
        try:
            nltk.download("punkt", quiet=True)
            logger.info("NLTK punkt downloaded successfully")
        except Exception as e:
            logger.warning("NLTK punkt download failed (non-fatal): %s", e)
    except Exception as e:
        logger.debug("NLTK unavailable in environment: %s", e)

def _short_hash(s: str, length: int = 12) -> str:
    try:
        from hashlib import sha256
        return sha256((s or "").encode("utf-8")).hexdigest()[:length]
    except Exception:
        return "hunk-err"

def create_app(config_object: Optional[str] = None) -> Flask:
    logger = setup_logging()
    logger.info("Starting PhishClassifier application")

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    template_dir = os.path.join(base_dir, "templates")
    static_dir = os.path.join(base_dir, "static")
    instance_path = os.path.join(base_dir, "instance")
    os.makedirs(instance_path, exist_ok=True)

    app = Flask(
        __name__,
        root_path=base_dir,
        template_folder=template_dir,
        static_folder=static_dir,
        static_url_path="/static",
        instance_path=instance_path,
        instance_relative_config=True
    )

    CORS(
        app,
        resources={
            r"/trainer/*": {
                "origins": ["http://localhost:5000", "http://127.0.0.1:5000"]
            }
        }
    )

    app.config.from_object("config.BaseConfig")
    if config_object:
        app.config.from_object(config_object)
        logger.info("Loaded config from %s", config_object)

    app.config.setdefault("DEBUG", True)
    app.config.setdefault("TESTING", False)
    app.config.setdefault("SECRET_KEY", "…your secret…")
    app.config.setdefault(
        "SQLALCHEMY_DATABASE_URI",
        "sqlite:///" + os.path.join(instance_path, "phishclassifier.db")
    )
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", 0)

    # REPORT_PATH default and ensure folder exists
    app.config.setdefault("REPORT_PATH", os.environ.get("REPORT_PATH", os.path.join(instance_path, "reports")))
    os.makedirs(app.config["REPORT_PATH"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    # Initialize socketio with configurable allowed origins and message queue
    # Allow override via SOCKETIO_ALLOWED_ORIGINS env var (comma-separated)
    _allowed = os.environ.get("SOCKETIO_ALLOWED_ORIGINS")
    if _allowed:
        cors_list = [u.strip() for u in _allowed.split(",") if u.strip()]
    else:
        # preserve original safe defaults and add the dev port that was being rejected
        cors_list = ["http://localhost:5000", "http://127.0.0.1:5000", "http://127.0.0.1:5001", "http://localhost:5001"]

    socketio.init_app(
        app,
        cors_allowed_origins=cors_list,
        message_queue=os.getenv("SOCKETIO_MESSAGE_QUEUE", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")),
        transports=["websocket", "polling"],
        logger=True,
        engineio_logger=True,
    )

    # Make SocketIO instance discoverable via current_app.extensions for helpers that expect it
    try:
        app.extensions["socketio"] = socketio
    except Exception:
        # non-fatal; leave as-is if extensions mapping not writable for some reason
        logger.debug("Failed to set app.extensions['socketio'] (non-fatal)", exc_info=True)

    _ensure_nltk_punkt(logger)

    @app.context_processor
    def inject_common():
        return {"current_year": datetime.utcnow().year, "prefix": request.path + ("?" if "?" not in request.path else "")}

    @app.route("/health", methods=["GET"])
    def health():
        return ("ok", 200)

    @app.route("/ready", methods=["GET"])
    def ready():
        try:
            from redis import Redis
            redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
            r = Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
            if not r.ping():
                return ("redis-unreachable", 503)
        except Exception as e:
            return (f"redis-error:{type(e).__name__}", 503)

        try:
            if "sqlalchemy" in current_app.extensions:
                engine = current_app.extensions["sqlalchemy"].db.engine
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            else:
                from sqlalchemy import create_engine
                db_url = os.environ.get("SQLALCHEMY_DATABASE_URI")
                if db_url:
                    engine = create_engine(db_url, connect_args={"connect_timeout": 1})
                    with engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
        except Exception as e:
            return (f"db-error:{type(e).__name__}", 503)

        return ("ready", 200)

    from flask import request as _fl_request  # alias

    # Existing trainer namespace handlers preserved
    @socketio.on('connect', namespace='/trainer')
    def on_trainer_connect():
        sid = getattr(_fl_request, "sid", None)
        print(f"[trainer namespace] client connected: {sid}")

    @socketio.on('disconnect', namespace='/trainer')
    def on_trainer_disconnect():
        sid = getattr(_fl_request, "sid", None)
        print(f"[trainer namespace] client disconnected: {sid}")

    # New: explicit reports namespace connect/disconnect handlers for better visibility
    @socketio.on('connect', namespace='/reports')
    def on_reports_connect():
        sid = getattr(_fl_request, "sid", None)
        current_app.logger.info("[reports namespace] client connected sid=%s remote=%s", sid, getattr(_fl_request, "remote_addr", "-"))

    @socketio.on('disconnect', namespace='/reports')
    def on_reports_disconnect():
        sid = getattr(_fl_request, "sid", None)
        current_app.logger.info("[reports namespace] client disconnected sid=%s remote=%s", sid, getattr(_fl_request, "remote_addr", "-"))

    with app.app_context():
        if os.environ.get("AUTO_CREATE_DB", "0").strip() == "1":
            db.create_all()
            logger.info("Auto-created DB schema because AUTO_CREATE_DB=1")
        logger.info("Database tables created or verified")
    init_db(app)

    app.register_blueprint(api_blueprint, url_prefix="/api")
    app.register_blueprint(reports_bp)
    app.register_blueprint(reports_ui)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(trainer_bp)
    register_commands(app)

    if PrometheusMetrics:
        PrometheusMetrics(app)
        logger.info("Prometheus metrics enabled via prometheus_flask_exporter")
    else:
        logger.info("prometheus_flask_exporter not installed or unavailable")

    try:
        import monitoring as monitoring
        monitoring.install_metrics_endpoint(app)
        logger.info("Installed monitoring metrics endpoint via .monitoring")
    except Exception:
        logger.exception("Failed to install .monitoring endpoint")

    # Guarded metrics route: only register if no existing 'metrics' endpoint present
    if "metrics" not in app.view_functions:
        @app.route("/metrics")
        def metrics():
            try:
                payload = generate_latest()
                return Response(payload, mimetype=CONTENT_TYPE_LATEST)
            except Exception:
                return Response(b"", mimetype="text/plain")
    else:
        # metrics endpoint already registered (e.g., by monitoring.install_metrics_endpoint)
        pass

    if tracer:
        try:
            tracer.configure()
            logger.info("Datadog tracer enabled")
        except Exception as e:
            logger.warning("Datadog tracer config failed: %s", e)
    else:
        logger.info("Datadog tracer not installed")

    Swagger(app)
    logger.info("Swagger UI enabled, routes:\n%s", app.url_map)

    @app.route("/")
    @app.route("/home")
    def home_page():
        return render_template("index.html")

    @app.route("/feedback", methods=["GET", "POST"])
    @login_required
    def submit_feedback():
        if request.method == "POST":
            content = request.form.get("feedback", "").strip()
            if not content:
                flash("Feedback cannot be empty.", "danger")
                return redirect(url_for("submit_feedback"))
            fb = Feedback(user_id=current_user.id, content=content)
            db.session.add(fb)
            db.session.commit()
            flash("Thank you for your feedback!", "success")
            return redirect(url_for("submit_feedback"))

        my_feedback = (
            Feedback.query
            .filter_by(user_id=current_user.id)
            .order_by(desc(Feedback.created_at))
            .all()
        )
        return render_template("feedback.html", my_feedback=my_feedback)

    @app.route("/admin_dashboard")
    @login_required
    @admin_required
    def admin_dashboard():
        items = Feedback.query.order_by(desc(Feedback.created_at)).all()
        return render_template("admin_dashboard.html", feedback_items=items)

    @app.route("/__log_export_click", methods=["POST"])
    @login_required
    @admin_required
    def log_export_click():
        try:
            payload = request.get_json(silent=True) or {}
            href = payload.get("href", "")
            safe_href = href.split("format=")[0] + ("format=" + href.split("format=")[-1] if "format=" in href else "")
            current_app.logger.info("EXPORT_CLICK user=%s href=%s", getattr(request, "remote_addr", "-"), safe_href)
            return jsonify({"status":"ok"}), 200
        except Exception:
            current_app.logger.exception("Failed to log export click")
            return jsonify({"status":"failed"}), 500

    @app.route("/admin/metrics")
    @login_required
    @admin_required
    def admin_metrics():
        total = Feedback.query.count()
        latest = (
            Feedback.query
            .order_by(desc(Feedback.created_at))
            .limit(5)
            .all()
        )
        stats = {
            "total_feedback": total,
            "active_users": User.query.count(),
            "prediction_success_rate": 0.95
        }
        return render_template(
            "admin_metrics.html",
            stats=stats,
            latest_feedback=latest
        )

    @app.route("/admin/users")
    @login_required
    @admin_required
    def admin_users():
        users = User.query.all()
        return render_template("admin_users.html", users=users)

    @app.route("/admin/users/edit/<int:user_id>", methods=["POST"])
    @login_required
    @admin_required
    def edit_user(user_id):
        try:
            current_app.logger.debug("REQUEST_FORM_ITEMS: %s", list(request.form.items()))
        except Exception:
            current_app.logger.exception("Failed to log request.form items")

        try:
            current_app.logger.debug("REQUEST_RAW_BODY: %r", (request.get_data(as_text=True) or "")[:2000])
        except Exception:
            current_app.logger.exception("Failed to log raw request body")

        user = User.query.get_or_404(user_id)
        form_is_admin = request.form.get("is_admin", "0")
        form_is_trainer = request.form.get("is_trainer")
        is_admin_val = None
        is_trainer_val = None

        if not request.form:
            try:
                raw = request.get_data(as_text=True) or ""
                current_app.logger.debug("FALLBACK_PARSE_RAW: %r", raw[:2000])
                from urllib.parse import parse_qs
                parsed = parse_qs(raw, keep_blank_values=True)
                def first(k):
                    return parsed.get(k, [None])[0]
                is_admin_val = first("is_admin")
                is_trainer_val = first("is_trainer")
            except Exception:
                current_app.logger.exception("FALLBACK_PARSE_FAILED")
                is_admin_val = None
                is_trainer_val = None

        if form_is_trainer is None:
            if is_trainer_val is not None:
                form_is_trainer = is_trainer_val
            else:
                try:
                    body = request.get_data(as_text=True) or ""
                    if "is_trainer=1" in body:
                        form_is_trainer = "1"
                    elif "is_trainer=0" in body:
                        form_is_trainer = "0"
                    else:
                        form_is_trainer = "0"
                except Exception:
                    form_is_trainer = "0"
        else:
            form_is_trainer = str(form_is_trainer)

        if form_is_admin is None:
            form_is_admin = is_admin_val or "0"
        else:
            form_is_admin = str(form_is_admin)

        new_is_admin = True if str(form_is_admin).lower() in ("1", "on", "true", "yes") else False
        new_is_trainer = True if str(form_is_trainer).lower() in ("1", "on", "true", "yes") else False

        if current_user.is_authenticated and current_user.id == user.id and not new_is_admin:
            flash("You cannot remove your own admin privileges.", "danger")
            return redirect(url_for("admin_users"))

        try:
            admin_role = None
            trainer_role = None
            if getattr(user, "has_role", None) and callable(user.has_role):
                try:
                    trainer_role = Role.query.filter_by(name="trainer").first()
                    admin_role = Role.query.filter_by(name="admin").first()
                except Exception:
                    trainer_role = None
                    admin_role = None
        except Exception:
            admin_role = None
            trainer_role = None

        try:
            if getattr(user, "add_role", None) and callable(user.add_role) and getattr(user, "remove_role", None) and callable(user.remove_role):
                if new_is_admin:
                    user.add_role(admin_role or "admin")
                else:
                    user.remove_role(admin_role or "admin")
            else:
                if hasattr(user, "is_admin"):
                    user.is_admin = new_is_admin
                else:
                    if getattr(user, "has_role", None) and callable(user.has_role):
                        has_admin = user.has_role("admin")
                        if new_is_admin and not has_admin:
                            try:
                                if getattr(user, "roles", None) is None:
                                    user.roles = ["admin"]
                                elif isinstance(user.roles, str):
                                    parts = [p.strip() for p in user.roles.split(",") if p.strip()]
                                    if "admin" not in parts:
                                        parts.append("admin")
                                    user.roles = ",".join(parts)
                                elif isinstance(user.roles, (list, tuple)):
                                    roles = list(user.roles)
                                    if "admin" not in roles:
                                        roles.append("admin")
                                    try:
                                        user.roles = roles
                                    except Exception:
                                        pass
                            except Exception:
                                current_app.logger.debug("Fallback admin add failed for user %s", user_id)
                        if not new_is_admin and has_admin:
                            try:
                                if isinstance(user.roles, str):
                                    parts = [p.strip() for p in user.roles.split(",") if p.strip() and p.strip() != "admin"]
                                    user.roles = ",".join(parts)
                                elif isinstance(user.roles, (list, tuple)):
                                    roles = [r for r in list(user.roles) if r != "admin"]
                                    try:
                                        user.roles = roles
                                    except Exception:
                                        pass
                            except Exception:
                                current_app.logger.debug("Fallback admin remove failed for user %s", user_id)
        except Exception:
            current_app.logger.exception("Failed to set admin flag for user %s", user_id)
            flash("Failed to update admin flag.", "danger")
            return redirect(url_for("admin_users"))

        try:
            if getattr(user, "add_role", None) and callable(user.add_role) and getattr(user, "remove_role", None) and callable(user.remove_role):
                if new_is_trainer and not trainer_role:
                    try:
                        trainer_role = Role.query.filter_by(name="trainer").first()
                        if not trainer_role:
                            trainer_role = Role(name="trainer")
                            db.session.add(trainer_role)
                            db.session.flush()
                    except Exception:
                        trainer_role = None

                if new_is_trainer:
                    user.add_role(trainer_role or "trainer")
                else:
                    user.remove_role(trainer_role or "trainer")
            else:
                if hasattr(user, "is_trainer"):
                    user.is_trainer = new_is_trainer
                elif hasattr(user, "trainer"):
                    user.trainer = new_is_trainer
                elif hasattr(user, "roles") and isinstance(getattr(user, "roles"), str):
                    roles = [r.strip() for r in (user.roles or "").split(",") if r.strip()]
                    if new_is_trainer and "trainer" not in roles:
                        roles.append("trainer")
                    if not new_is_trainer and "trainer" in roles:
                        roles = [r for r in roles if r != "trainer"]
                    user.roles = ",".join(roles)
                elif hasattr(user, "roles") and isinstance(getattr(user, "roles"), (list, tuple)):
                    roles = list(user.roles)
                    if new_is_trainer and "trainer" not in roles:
                        roles.append("trainer")
                    if not new_is_trainer and "trainer" in roles:
                        roles = [r for r in roles if r != "trainer"]
                    try:
                        user.roles = roles
                    except Exception:
                        current_app.logger.debug("Could not assign list to user.roles for user %s", user_id)
        except Exception:
            current_app.logger.exception("Failed to toggle trainer role for user %s", user_id)
            flash("Failed to update trainer role.", "danger")
            return redirect(url_for("admin_users"))

        try:
            if hasattr(user, "is_admin"):
                user.is_admin = bool(new_is_admin)
            if hasattr(user, "is_trainer"):
                user.is_trainer = bool(new_is_trainer)
        except Exception:
            current_app.logger.exception("Failed to sync boolean flags for user %s", user_id)

        try:
            db.session.add(user)
            db.session.commit()
            flash(f"Roles updated for {user.username}", "success")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to commit role changes for user %s", user_id)
            flash("Failed to save role changes.", "danger")

        return redirect(url_for("admin_users"))

    @app.route("/respond_feedback/<int:feedback_id>", methods=["POST"])
    @login_required
    @admin_required
    def respond_feedback_admin(feedback_id: int):
        fb = Feedback.query.get_or_404(feedback_id)
        resp = request.form.get("response", "").strip()
        if resp:
            fb.admin_response = resp
            db.session.commit()
            flash("Response sent.", "success")
        else:
            flash("Response cannot be empty.", "danger")
        return redirect(url_for("admin_dashboard"))

    @app.route("/quarantine")
    @login_required
    @admin_required
    def quarantine_page():
        start_str = request.args.get("start_date", "")
        end_str = request.args.get("end_date", "")
        min_score = request.args.get("min_score", type=float)
        max_score = request.args.get("max_score", type=float)
        domain = request.args.get("domain", type=str, default="").strip()
        q_param = request.args.get("q", "").strip()

        iso_fmt = "%Y-%m-%d"
        start_date = datetime.strptime(start_str, iso_fmt) if start_str else None
        end_date = datetime.strptime(end_str, iso_fmt) if end_str else None

        q = QuarantinedEmail.query
        if start_date:
            q = q.filter(QuarantinedEmail.received_at >= start_date)
        if end_date:
            end_inclusive = end_date + timedelta(days=1) - timedelta(microseconds=1)
            q = q.filter(QuarantinedEmail.received_at <= end_inclusive)

        if min_score is not None and max_score is not None:
            if min_score > max_score:
                min_score, max_score = max_score, min_score
            q = q.filter(QuarantinedEmail.risk_score.between(min_score, max_score))
        elif min_score is not None:
            q = q.filter(QuarantinedEmail.risk_score >= min_score)
        elif max_score is not None:
            q = q.filter(QuarantinedEmail.risk_score <= max_score)

        if domain:
            q = q.filter(
                QuarantinedEmail.sender.ilike(f"%{domain}%")
            )

        if q_param:
            pat = f"%{q_param}%"
            q = q.filter(
                (QuarantinedEmail.subject.ilike(pat)) |
                (QuarantinedEmail.body.ilike(pat))
            )

        fmt = request.args.get("format")
        if fmt in ("csv", "json"):
            try:
                items_q = q.order_by(desc(QuarantinedEmail.received_at)).all()
                items = []
                for r in items_q:
                    items.append({
                        "id": r.id,
                        "sender": getattr(r, "sender", "") or "",
                        "subject": getattr(r, "subject", "") or "",
                        "received_at": (r.received_at.isoformat() if getattr(r, "received_at", None) else ""),
                        "risk_score": getattr(r, "risk_score", None),
                        "feedback": getattr(r, "feedback", "") or ""
                    })
            except Exception as e:
                current_app.logger.exception("Failed to build export items: %s", e)
                return jsonify({"error": "export failed"}), 500

            if fmt == "csv":
                import csv, io
                buf = io.StringIO()
                writer = csv.writer(buf)
                headers = ["id", "sender", "subject", "received_at", "risk_score", "feedback"]
                writer.writerow(headers)
                for it in items:
                    writer.writerow([it.get(h, "") for h in headers])
                resp = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
                resp.headers["Content-Disposition"] = 'attachment; filename="quarantine.csv"'
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                current_app.logger.info("Export CSV: returned %d rows", len(items))
                return resp

            if fmt == "json":
                resp = jsonify(items)
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                current_app.logger.info("Export JSON: returned %d rows", len(items))
                return resp

        try:
            pagination = (
                q.order_by(desc(QuarantinedEmail.received_at))
                 .paginate(
                     page=request.args.get("page", 1, type=int),
                     per_page=request.args.get("per_page", 20, type=int),
                     error_out=False
                 )
            )

            emails = pagination.items
            for e in emails:
                try:
                    body = getattr(e, "body", "") or ""
                    e._sentences = safe_sent_tokenize(body)[:6]
                except Exception:
                    e._sentences = []

            filters_active = any([start_str, end_str, min_score is not None, max_score is not None, domain, q_param])

            args_dict = dict(request.args) if request.args else {}
            csv_args = args_dict.copy()
            csv_args['format'] = 'csv'
            csv_url = url_for('quarantine_page', **csv_args)
            json_args = dict(request.args) if request.args else {}
            json_args['format'] = 'json'
            json_url = url_for('quarantine_page', **json_args)

            return render_template(
                "quarantine.html",
                emails=emails,
                pagination=pagination,
                filters=request.args,
                filters_active=filters_active,
                csv_url=csv_url,
                json_url=json_url
            )
        except LookupError as le:
            current_app.logger.exception("Quarantine pagination LookupError (likely invalid enum values): %s", le)
            try:
                raw_rows = db.session.execute(
                    sa_text(
                        "SELECT id, feedback FROM quarantined_emails WHERE feedback IS NULL OR feedback = '' OR feedback NOT IN ('none','false_positive','false_negative')"
                    )
                ).fetchall()
                current_app.logger.warning("Offending quarantined_emails rows: %s", raw_rows)
            except Exception:
                current_app.logger.exception("Failed to query raw offending rows.")
            return render_template("500.html"), 500
        except Exception as exc:
            current_app.logger.exception("Unexpected error rendering quarantine: %s", exc)
            return render_template("500.html"), 500

    @app.route("/predict_form", methods=["GET"])
    @login_required
    def predict_form():
        form = EmailForm()
        if form.validate_on_submit():
            from tasks import classify_email_task

            # Best-effort optimistic TaskAudit creation and socket emit so UI shows the job
            audit_id = None
            audit_row_id = None
            try:
                try:
                    from models.task_audit import TaskAudit
                    sess = db.session
                    ta = TaskAudit(
                        task_type="classify",
                        task_id=None,
                        user=getattr(request, "remote_addr", "-"),
                        payload={"summary": "classify email (ui submit)"},
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
                    # capture DB primary id explicitly and use it for task linkage
                    audit_row_id = getattr(ta, "id", None)
                    audit_id = audit_row_id or getattr(ta, "task_id", None)
                except Exception:
                    current_app.logger.debug("predict_form: optimistic TaskAudit creation skipped")
            except Exception:
                current_app.logger.exception("predict_form: optimistic audit creation non-fatal")
                audit_id = None
                audit_row_id = None

            try:
                # Pass DB audit_row_id if available for deterministic linkage
                try:
                    current_app.logger.info("Coordinator: attempting to delegate classify_email_task for audit_id=%s (predict_form)", audit_row_id)
                    async_res = classify_email_task.delay(form.email.data, audit_id=audit_row_id)
                    task_id = getattr(async_res, "id", None)
                    current_app.logger.info("Coordinator: classify_email_task.delay returned async_id=%s for audit_id=%s (predict_form)", task_id, audit_row_id)
                except Exception:
                    current_app.logger.exception("Coordinator: failed to delegate classify_email_task for audit_id=%s (predict_form)", audit_row_id)
                    # fallback synchronous attempt
                    try:
                        classify_email_task(form.email.data)
                    except Exception:
                        pass
                    task_id = None
            except Exception:
                task_id = None

            # emit optimistic created event
            try:
                payload_out = {
                    "task_id": task_id,
                    "task_type": "classify",
                    "summary": "classify email (ui submit)",
                    "user": getattr(request, "remote_addr", "-"),
                    "audit_id": audit_row_id,
                    "created_at": datetime.utcnow().isoformat(),
                    "status": "pending",
                }
                socketio.emit("task:created", payload_out, namespace="/reports")
            except Exception:
                current_app.logger.debug("predict_form: socket emit task:created failed (non-fatal)")

            flash("Email submitted for classification.", "success")
            return redirect(url_for("home_page"))

        return render_template("predict_form.html", form=form)

    @app.route("/classify", methods=["POST"])
    @login_required
    def classify_email():
        try:
            data = request.get_json(silent=True) or request.form.to_dict() or {}
            if not data:
                raw = request.get_data(as_text=True) or ""
                if raw:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {}
        except Exception:
            data = {}

        email_id = data.get("email_id")
        content = (data.get("content") or data.get("text") or data.get("email") or "").strip()

        if not content:
            return jsonify({"error": "Email content is required."}), 400

        # Best-effort optimistic TaskAudit creation and socket emit so UI shows the job
        audit_id = None
        audit_row_id = None
        try:
            try:
                from models.task_audit import TaskAudit
                sess = db.session
                ta = TaskAudit(
                    task_type="classify",
                    task_id=None,
                    user=getattr(request, "remote_addr", "-"),
                    payload={"summary": "classify email"},
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
                # capture DB primary id explicitly
                audit_row_id = getattr(ta, "id", None)
                audit_id = audit_row_id or getattr(ta, "task_id", None)
            except Exception:
                current_app.logger.debug("classify_email: optimistic TaskAudit creation skipped")
        except Exception:
            current_app.logger.exception("classify_email: optimistic audit creation non-fatal")
            audit_id = None
            audit_row_id = None

        try:
            from tasks import classify_email_task
            # pass the DB id (audit_row_id) when available to keep linkage deterministic
            try:
                current_app.logger.info("Coordinator: attempting to delegate classify_email_task for audit_id=%s (classify_email)", audit_row_id)
                async_result = classify_email_task.delay(email_id, content, audit_id=audit_row_id)
                task_id = getattr(async_result, "id", None)
                current_app.logger.info("Coordinator: classify_email_task.delay returned async_id=%s for audit_id=%s (classify_email)", task_id, audit_row_id)
            except Exception:
                current_app.logger.exception("Coordinator: failed to delegate classify_email_task for audit_id=%s (classify_email)", audit_row_id)
                # fallback synchronous attempt
                try:
                    classify_email_task(email_id, content)
                except Exception:
                    pass
                task_id = None

            # Emit optimistic created event to socket clients (best-effort)
            try:
                payload_out = {
                    "task_id": task_id,
                    "task_type": "classify",
                    "summary": "classify email",
                    "user": getattr(request, "remote_addr", "-"),
                    "audit_id": audit_row_id,
                    "created_at": datetime.utcnow().isoformat(),
                    "status": "pending",
                }
                socketio.emit("task:created", payload_out, namespace="/reports")
            except Exception:
                current_app.logger.debug("classify_email: socket emit task:created failed (non-fatal)")

            return jsonify({
                "status": "queued",
                "task_id": async_result.id if 'async_result' in locals() and async_result is not None else None,
                "audit_id": audit_row_id
            }), 202
        except Exception as e:
            current_app.logger.exception("Failed to enqueue classify task: %s", e)
            return jsonify({"error": "Enqueue failed", "details": str(e)}), 500

    @app.route("/api/classify", methods=["POST"])
    def api_classify():
        try:
            data = request.get_json(silent=True) or request.form.to_dict() or {}
            if not data:
                raw = request.get_data(as_text=True) or ""
                if raw:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {}
        except Exception:
            data = {}

        email_text = (data.get("content") or data.get("text") or data.get("email") or "").strip()
        if not email_text:
            return jsonify({"error": "Email content is required."}), 400

        try:
            from api.classifier import classify_text
            result = classify_text(email_text)
            if not isinstance(result, dict):
                return jsonify({"error": "Classifier returned unexpected result type."}), 500

            pred_label = result.get("prediction") or result.get("label") or result.get("prediction_result")
            risk = result.get("risk_score") or result.get("score")

            try:
                risk_val = float(risk) if risk is not None else None
            except Exception:
                risk_val = None

            try:
                lab = (pred_label or "")
                lab = lab.lower().strip() if isinstance(lab, str) else ""

                clf_thresh = None
                try:
                    if result.get("threshold") is not None:
                        clf_thresh = float(result.get("threshold"))
                    elif result.get("threshold_percent") is not None:
                        clf_thresh = float(result.get("threshold_percent")) / 100.0
                except Exception:
                    clf_thresh = None

                file_thresh = None
                try:
                    import os as _os, json as _json
                    tpath = _os.path.join(current_app.root_path, "thresholds.json")
                    if _os.path.exists(tpath):
                        with open(tpath, "r", encoding="utf-8") as fh:
                            j = _json.load(fh)
                        file_thresh = (
                            j.get("F1.0", {})
                             .get("WeightedGlobal")
                        )
                        if file_thresh is not None:
                            file_thresh = float(file_thresh)
                except Exception:
                    file_thresh = None

                config_thresh = float(current_app.config.get("QUARANTINE_RISK_THRESHOLD", 0.5))

                threshold_to_use = clf_thresh if clf_thresh is not None else (file_thresh if file_thresh is not None else config_thresh)

                is_quarantined = False
                if lab and any(tok in lab for tok in ("phish", "phishing", "quarantine", "quarantined", "spam")):
                    is_quarantined = True
                elif risk_val is not None and risk_val >= threshold_to_use:
                    is_quarantined = True

                try:
                    sender_val = ""
                    if isinstance(data, dict):
                        sender_val = (data.get("sender") or "").strip()
                    if not sender_val:
                        sender_val = "unknown@missing"
                except Exception:
                    sender_val = "unknown@missing"

                from api.persistence import save_prediction

                persistence = save_prediction(
                    sender=sender_val,
                    subject=((data.get("subject") or "")[:1024]) if isinstance(data, dict) else (result.get("subject","") or ""),
                    body=email_text or "",
                    received_at=datetime.utcnow(),
                    risk_score=risk_val,
                    status="quarantined" if is_quarantined else "scored",
                    feedback=None,
                    prediction=pred_label,
                    technical_explanation=result.get("technical_explanation"),
                    attacker_insights=result.get("attacker_insights"),
                    link_analysis=result.get("link_analysis"),
                    model_version=result.get("model_version"),
                    attachments_meta=result.get("attachments_meta"),
                    create_audit=False
                )

                # Best-effort: write quick artifacts so Reports UI can preview immediately
                try:
                    report_dir = current_app.config.get("REPORT_PATH") or os.path.join(current_app.instance_path, "reports")
                    os.makedirs(report_dir, exist_ok=True)
                    pid = None
                    if isinstance(persistence, dict):
                        pid = persistence.get("id") or persistence.get("persistence_id")
                    if pid is not None:
                        ts = datetime.utcnow().isoformat()

                        # Append a durable log line for this persistence id
                        try:
                            log_path = os.path.join(report_dir, f"{pid}.log")
                            with open(log_path, "a", encoding="utf-8") as fh:
                                fh.write(f"[{ts}] INFO persisted id={pid} from web\n")
                                fh.flush()
                                try:
                                    os.fsync(fh.fileno())
                                except Exception:
                                    pass
                            try:
                                os.chmod(log_path, 0o600)
                            except Exception:
                                pass
                        except Exception:
                            pass

                        # Write a raw body artifact (canonical short raw)
                        try:
                            raw_body_path = os.path.join(report_dir, f"{pid}.body.txt")
                            preview = (email_text or "")[:100000]
                            with open(raw_body_path, "w", encoding="utf-8") as fh:
                                fh.write(preview)
                                fh.flush()
                                try:
                                    os.fsync(fh.fileno())
                                except Exception:
                                    pass
                            try:
                                os.chmod(raw_body_path, 0o600)
                            except Exception:
                                pass
                            # best-effort chown normalization
                            try:
                                owner_uid = int(os.environ.get("REPORT_OWNER_UID", "1000"))
                                owner_gid = int(os.environ.get("REPORT_OWNER_GID", "1000"))
                                try:
                                    os.chown(raw_body_path, owner_uid, owner_gid)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        except Exception:
                            pass

                        # Write a full body preview artifact used by UI immediately
                        try:
                            full_path = os.path.join(report_dir, f"{pid}.full.body.txt")
                            preview = (email_text or "")[:100000]
                            with open(full_path, "w", encoding="utf-8") as fh:
                                fh.write(preview)
                                fh.flush()
                                try:
                                    os.fsync(fh.fileno())
                                except Exception:
                                    pass
                            try:
                                os.chmod(full_path, 0o600)
                            except Exception:
                                pass
                            # best-effort chown normalization
                            try:
                                owner_uid = int(os.environ.get("REPORT_OWNER_UID", "1000"))
                                owner_gid = int(os.environ.get("REPORT_OWNER_GID", "1000"))
                                try:
                                    os.chown(full_path, owner_uid, owner_gid)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        except Exception:
                            pass
                except Exception:
                    pass

                if persistence.get("status") != "ok":
                    current_app.logger.warning("api_classify: persistence indicated failure: %s", persistence.get("error"))
                else:
                    current_app.logger.info("quarantine:persisted sender=%s id=%s score=%s", sender_val, persistence.get("id"), risk_val)

            except Exception:
                current_app.logger.exception("Failed to persist prediction record")
                try:
                    db.session.rollback()
                except Exception:
                    pass

            try:
                email_counter.inc()
                if isinstance(risk_val, (int, float)):
                    risk_hist.observe(float(risk_val))
            except Exception:
                pass

            return jsonify(result), 200
        except Exception as e:
            current_app.logger.exception("api_classify failed: %s", e)
            return jsonify({"error": "Classification failed", "details": str(e)}), 500

    @app.route("/api/enqueue_classify", methods=["POST"])
    def api_enqueue_classify():
        try:
            data = request.get_json(silent=True) or request.form.to_dict() or {}
            if not data:
                raw = request.get_data(as_text=True) or ""
                if raw:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {}
        except Exception:
            data = {}

        email_id = data.get("email_id")
        content = (data.get("content") or data.get("text") or data.get("email") or "").strip()
        if not content:
            return jsonify({"error": "Email content is required."}), 400

        # optimistic TaskAudit creation for API enqueue as well
        audit_id = None
        audit_row_id = None
        try:
            try:
                from models.task_audit import TaskAudit
                sess = db.session
                ta = TaskAudit(
                    task_type="classify",
                    task_id=None,
                    user=getattr(request, "remote_addr", "-"),
                    payload={"summary": "classify email (api_enqueue)"},
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
                audit_row_id = getattr(ta, "id", None)
                audit_id = audit_row_id or getattr(ta, "task_id", None)
            except Exception:
                current_app.logger.debug("api_enqueue_classify: optimistic TaskAudit creation skipped")
        except Exception:
            current_app.logger.exception("api_enqueue_classify: optimistic audit creation non-fatal")
            audit_id = None
            audit_row_id = None

        try:
            from tasks import classify_email_task
            # pass DB id for deterministic linkage when available
            try:
                current_app.logger.info("Coordinator: attempting to delegate classify_email_task for audit_id=%s (api_enqueue_classify)", audit_row_id)
                async_result = classify_email_task.delay(email_id, content, audit_id=audit_row_id)
                task_id = getattr(async_result, "id", None)
                current_app.logger.info("Coordinator: classify_email_task.delay returned async_id=%s for audit_id=%s (api_enqueue_classify)", task_id, audit_row_id)
            except Exception:
                current_app.logger.exception("Coordinator: failed to delegate classify_email_task for audit_id=%s (api_enqueue_classify)", audit_row_id)
                # fallback synchronous
                try:
                    classify_email_task(email_id, content)
                except Exception:
                    pass
                task_id = None

            # attach task_id back to TaskAudit row if we created one
            if audit_row_id is not None:
                try:
                    sess = db.session
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
                    current_app.logger.exception("api_enqueue_classify: failed to attach task_id to TaskAudit (non-fatal)")

            # Emit optimistic created event
            try:
                payload_out = {
                    "task_id": task_id,
                    "task_type": "classify",
                    "summary": "classify email (api_enqueue)",
                    "user": getattr(request, "remote_addr", "-"),
                    "audit_id": audit_row_id,
                    "created_at": datetime.utcnow().isoformat(),
                    "status": "pending",
                }
                socketio.emit("task:created", payload_out, namespace="/reports")
            except Exception:
                current_app.logger.debug("api_enqueue_classify: socket emit task:created failed (non-fatal)")

            return jsonify({"status": "queued", "task_id": async_result.id if 'async_result' in locals() and async_result is not None else None, "audit_id": audit_row_id}), 202
        except Exception as e:
            current_app.logger.exception("api_enqueue_classify failed: %s", e)
            return jsonify({"error": "Enqueue failed", "details": str(e)}), 500

    @app.route("/analysis_data", methods=["GET"])
    def analysis_data():
        try:
            if "sqlalchemy" in current_app.extensions:
                rows = (
                    QuarantinedEmail.query
                    .order_by(QuarantinedEmail.received_at.desc())
                    .limit(50)
                    .all()
                )
                rows = list(reversed(rows))

                labels = [
                    (r.received_at.isoformat(timespec="minutes") if getattr(r, "received_at", None) else "?")
                    for r in rows
                ]

                risk_scores = []
                for r in rows:
                    rs = getattr(r, "risk_score", None)
                    if rs is None:
                        risk_scores.append(None)
                    else:
                        try:
                            risk_scores.append(float(rs) * 100.0)
                        except Exception:
                            risk_scores.append(None)

                row_summaries = []
                for r, lbl, score in zip(rows, labels, risk_scores):
                    top_links = []
                    la_json = None
                    try:
                        if getattr(r, "link_analysis", None):
                            la_json = json.loads(r.link_analysis)
                    except Exception:
                        la_json = None
                    if la_json and isinstance(la_json, list):
                        try:
                            sorted_la = sorted(la_json, key=lambda x: x.get("suspicion", x.get("suspicion_percent", 0)), reverse=True)[:3]
                        except Exception:
                            sorted_la = la_json[:3]
                        for l in sorted_la:
                            top_links.append({
                                "host": l.get("host", ""),
                                "suspicion_percent": l.get("suspicion_percent", 0)
                            })

                    computed_at = None
                    if getattr(r, "computed_at", None):
                        try:
                            computed_at = r.computed_at.isoformat(timespec="minutes")
                        except Exception:
                            computed_at = None
                    if computed_at is None and getattr(r, "received_at", None):
                        try:
                            computed_at = r.received_at.isoformat(timespec="minutes")
                        except Exception:
                            computed_at = None

                    row_summaries.append({
                        "id": r.id,
                        "label": lbl,
                        "risk_score": None if score is None else round(score, 1),
                        "computed_at": computed_at,
                        "top_links": top_links
                    })

                return jsonify({
                    "labels": list(labels),
                    "risk_scores": list(risk_scores),
                    "rows": row_summaries
                }), 200
        except Exception as exc:
            current_app.logger.exception("Failed to build analysis_data from DB: %s", exc)

        return jsonify({"labels": [], "risk_scores": [], "rows": []}), 200

    try:
        from celery.result import AsyncResult
    except Exception:
        AsyncResult = None

    @app.route("/task_status/<task_id>", methods=["GET"])
    @login_required
    def task_status(task_id):
        if AsyncResult is None:
            return jsonify({"error": "Celery result backend unavailable"}), 503

        res = AsyncResult(task_id)
        payload = {"state": res.state}
        if res.state == "SUCCESS":
            payload["result"] = res.result
        elif res.state == "FAILURE":
            payload["error"] = str(res.result)
        return jsonify(payload), 200

    @app.route("/quarantine/release/<int:qid>", methods=["POST"])
    @login_required
    @admin_required
    def release_email(qid: int):
        e = QuarantinedEmail.query.get_or_404(qid)
        try:
            e.status = "released"
            db.session.commit()
            flash(f"Email {qid} released.", "success")
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "ok", "qid": qid}), 200
        except Exception:
            db.session.rollback()
            flash("Failed to release email.", "danger")
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "failed"}), 500
        return redirect(url_for("quarantine_page"))

    @app.route("/quarantine/delete/<int:qid>", methods=["POST"])
    @login_required
    @admin_required
    def delete_email(qid: int):
        e = QuarantinedEmail.query.get_or_404(qid)
        try:
            db.session.delete(e)
            db.session.commit()
            flash(f"Email {qid} deleted.", "warning")
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "ok", "qid": qid}), 200
        except Exception:
            db.session.rollback()
            flash("Failed to delete email.", "danger")
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "failed"}), 500
        return redirect(url_for("quarantine_page"))

    @app.route("/bulk/release", methods=["POST"])
    @login_required
    @admin_required
    def bulk_release():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", []) if isinstance(data, dict) else []
        if not ids:
            return jsonify({"error": "no ids"}), 400
        try:
            rows = QuarantinedEmail.query.filter(QuarantinedEmail.id.in_(ids)).all()
            for r in rows:
                r.status = "released"
            db.session.commit()
            return jsonify({"status": "ok", "count": len(rows)}), 200
        except Exception:
            db.session.rollback()
            current_app.logger.exception("bulk release failed")
            return jsonify({"status": "failed"}), 500

    @app.route("/bulk/delete", methods=["POST"])
    @login_required
    @admin_required
    def bulk_delete():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", []) if isinstance(data, dict) else []
        if not ids:
            return jsonify({"error": "no ids"}), 400
        try:
            rows = QuarantinedEmail.query.filter(QuarantinedEmail.id.in_(ids)).all()
            deleted = len(rows)
            for r in rows:
                db.session.delete(r)
            db.session.commit()
            return jsonify({"status": "ok", "count": deleted}), 200
        except Exception:
            db.session.rollback()
            current_app.logger.exception("bulk delete failed")
            return jsonify({"status": "failed"}), 500

    @app.route("/quarantine/feedback/<int:qid>", methods=["POST"])
    @login_required
    @admin_required
    def quarantine_feedback(qid: int):
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        val = (data.get("feedback") or data.get("value") or "").strip()
        allowed = ("none", "false_positive", "false_negative")
        if not val:
            return jsonify({"error": "missing feedback"}), 400
        if val not in allowed:
            return jsonify({"error": "invalid feedback"}), 400
        e = QuarantinedEmail.query.get_or_404(qid)
        old = e.feedback
        e.feedback = val
        try:
            try:
                fa = FeedbackAudit(quarantine_id=qid, user_id=getattr(current_user, "id", None), old_value=old, new_value=val)
                db.session.add(fa)
            except Exception as inner:
                import traceback
                current_app.logger.error("Failed creating FeedbackAudit: %s\n%s", inner, traceback.format_exc())
            db.session.commit()
            return jsonify({"status": "ok", "feedback": val}), 200
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to save feedback for %s", qid)
            return jsonify({"status": "failed"}), 500

    @app.route("/_debug/probe_rows", methods=["POST"])
    @login_required
    @admin_required
    def _debug_probe_rows():
        data = request.get_json(silent=True) or {}
        needle = data.get("needle", "")
        limit = int(data.get("limit", 20))
        if not needle:
            return jsonify({"error": "needle required"}), 400
        pattern = f"%{needle}%"
        try:
            rows = db.session.execute(sa_text(
                "SELECT id, sender, subject, substr(body,1,200) as body_preview, prediction, risk_score, created_at FROM quarantined_emails WHERE body LIKE :p ORDER BY created_at DESC LIMIT :lim"
            ), {"p": pattern, "lim": limit}).fetchall()
            out = [{
                "id": r.id,
                "sender": r.sender,
                "subject_hash": _short_hash(r.subject or ""),
                "body_preview": (r.body_preview or "")[:200],
                "prediction": r.prediction,
                "risk_score": r.risk_score,
                "created_at": str(r.created_at)
            } for r in rows]
            current_app.logger.info("probe:found %d rows needle=%s", len(out), needle)
            return jsonify({"count": len(out), "rows": out}), 200
        except Exception:
            current_app.logger.exception("probe:query-failed needle=%s", needle)
            return jsonify({"error": "probe failed"}), 500

    @app.errorhandler(404)
    def not_found(error):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template("500.html"), 500

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            if getattr(current_user, "is_admin", False):
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('home_page'))

        next_url = request.args.get('next', '')
        if request.method == 'POST':
            next_url = request.form.get('next', next_url)
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')

            user = User.query.filter_by(username=username).first()
            if user and getattr(user, "check_password", None) and user.check_password(password):
                login_user(user)
                if next_url and is_safe_url(next_url):
                    return redirect(next_url)
                if getattr(user, "is_admin", False):
                    return redirect(url_for('admin_dashboard'))
                return redirect(url_for('home_page'))

            flash('Invalid credentials', 'danger')
            return redirect(url_for('login', next=next_url))

        return render_template('login.html', next=next_url)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for('admin_dashboard'))

        username = request.form.get('username', '').strip()
        pw1 = request.form.get('password', '')
        pw2 = request.form.get('confirm_password', '')
        next_url = request.form.get('next', '')

        if not username or not pw1 or pw1 != pw2:
            flash('Registration error; check fields.', 'danger')
            return redirect(url_for('login', next=next_url))

        hashed = generate_password_hash(pw1)
        new_user = User(username=username, password=hashed)
        db.session.add(new_user)
        db.session.commit()
        flash('Registered! Please log in.', 'success')
        return redirect(url_for('login', next=next_url))

    @app.route('/logout', methods=['GET'])
    def logout():
        logout_user()
        flash('You have been logged out.', 'info')
        return redirect(url_for("login"))

    return app


if __name__ == "__main__":
    app = create_app()
    socketio.run(
         app,
         host="0.0.0.0",
         port=int(os.environ.get("PORT", 5000)),
         debug=True
     )
