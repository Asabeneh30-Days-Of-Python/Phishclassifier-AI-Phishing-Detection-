# config.py

import os
from pathlib import Path
from sqlalchemy.pool import NullPool

# 1) Locate project root and load .env (if you use python-dotenv)
BASE_DIR = Path(__file__).resolve().parent
dotenv_path = BASE_DIR / '.env'
if dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path)

class BaseConfig:
    # 2) Secret key for session signing
    SECRET_KEY = os.environ.get(
        "SECRET_KEY",
        "9a7bcf318f0b44f028f701d02ca43599a526e37ae2711d201cc507b9377cd65b"
    )

    # 3) Database settings — prefer SQLALCHEMY_DATABASE_URI, then DATABASE_URL, then a repo-local sqlite path
    _env_db = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ.get("DATABASE_URL")
    if _env_db:
        SQLALCHEMY_DATABASE_URI = _env_db
    else:
        # create a platform-correct absolute sqlite URL pointing at ./instance/phishclassifier.db
        instance_path = Path(BASE_DIR) / "instance" / "phishclassifier.db"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{instance_path.as_posix()}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 4) Disable file caching in dev
    SEND_FILE_MAX_AGE_DEFAULT = 0

    # 5) Disable pooling on SQLite to avoid threading-lock errors in Celery
    SQLALCHEMY_ENGINE_OPTIONS = {
         "poolclass": NullPool,
         "connect_args": {
             "check_same_thread": False
         }
    }

    #
    # Celery / broker / results
    #
    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
    ENRICHMENT_QUEUE = os.environ.get("ENRICHMENT_QUEUE", "cpu")

    #
    # Google Safe Browsing (Lookup)
    #
    ENABLE_GSB = os.environ.get("ENABLE_GSB", "0").lower() in ("1", "true", "yes", "on")
    GSB_API_KEY = os.environ.get("GSB_API_KEY")
    GSB_CLIENT = os.environ.get("GSB_CLIENT", "phishclassifier")
    GSB_TIMEOUT = float(os.environ.get("GSB_TIMEOUT", "3"))
    GSB_MAX_RETRIES = int(os.environ.get("GSB_MAX_RETRIES", "3"))
    GSB_CACHE_TTL = int(os.environ.get("GSB_CACHE_TTL", "86400"))

    #
    # Urlscan.io config
    #
    ENABLE_URLSCAN = os.environ.get("ENABLE_URLSCAN", "0") in ("1", "true", "True", "yes", "on")
    URLSCAN_API_KEY = os.environ.get("URLSCAN_API_KEY")
    URLSCAN_TIMEOUT = float(os.environ.get("URLSCAN_TIMEOUT", "5"))
    URLSCAN_POLL_DELAY = float(os.environ.get("URLSCAN_POLL_DELAY", "1"))
    URLSCAN_MAX_POLL_RETRIES = int(os.environ.get("URLSCAN_MAX_POLL_RETRIES", "8"))
    URLSCAN_PUBLIC = os.environ.get("URLSCAN_PUBLIC", "off")  # off keeps scans private
    URLSCAN_CONCURRENT_SUBMITS = int(os.environ.get("URLSCAN_CONCURRENT_SUBMITS", "3"))
    URLSCAN_SUBMIT_RATE_PER_MIN = int(os.environ.get("URLSCAN_SUBMIT_RATE_PER_MIN", "30"))

    #
    # VirusTotal (optional)
    #
    ENABLE_VT = os.environ.get("ENABLE_VT", "0") in ("1", "true", "True", "yes", "on")
    VT_API_KEY = os.environ.get("VT_API_KEY") or os.environ.get("VIRUSTOTAL_API_KEY") or os.environ.get("VIRUSTOTAL_KEY")

    #
    # PhishTank (optional, disabled by default)
    #
    PHISHTANK_KEY = os.environ.get("PHISHTANK_KEY", "") or ""
    PHISHTANK_TIMEOUT = float(os.environ.get("PHISHTANK_TIMEOUT", "5"))
    PHISHTANK_MAX_RETRIES = int(os.environ.get("PHISHTANK_MAX_RETRIES", "3"))
    PHISHTANK_TTL_SAFE = int(os.environ.get("PHISHTANK_TTL_SAFE", "86400"))
    PHISHTANK_TTL_PHISH = int(os.environ.get("PHISHTANK_TTL_PHISH", "259200"))

    #
    # Caching (Redis)
    #
    CACHE_BACKEND = os.environ.get("CACHE_BACKEND", "redis")
    CACHE_URL = os.environ.get("CACHE_URL", "redis://localhost:6379/1")
    CACHE_TTL_URLSCAN = int(os.environ.get("CACHE_TTL_URLSCAN", "2592000"))  # 30 days

    #
    # Enrichment timeouts and limits
    #
    ENRICHMENT_PER_URL_TIMEOUT = float(os.environ.get("ENRICHMENT_PER_URL_TIMEOUT", "4"))
    ENRICHMENT_MAX_REDIRECTS = int(os.environ.get("ENRICHMENT_MAX_REDIRECTS", "5"))

    #
    # Monitoring and safety
    #
    METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "true") in ("1", "true", "True", "yes", "on")
    SENTRY_DSN = os.environ.get("SENTRY_DSN", "")

class DevelopmentConfig(BaseConfig):
    DEBUG = True

class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True