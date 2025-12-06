# preload_model.py
"""
Lightweight model preload helper.

Usage:
- Call get_model() at module import time or from a startup script to load the model
  into this module's process memory. Prefork workers that import tasks.py after
  this will inherit the warmed model.
- get_model() is idempotent and thread-safe.

Configuration:
- MODEL_PATH env var (defaults to /app/models/phishing_model.pkl)
- Supports pickle and joblib files based on file extension.
"""
from __future__ import annotations

import os
import threading
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("phishclassifier.preload")

_model_lock = threading.Lock()
_model: Optional[Any] = None
_model_path_cached: Optional[str] = None

def _load_with_pickle(path: Path):
    import pickle

    with path.open("rb") as fh:
        return pickle.load(fh)

def _load_with_joblib(path: Path):
    try:
        import joblib
    except Exception:
        # joblib may be installed as part of scikit-learn or separately
        raise
    return joblib.load(str(path))

def _resolve_model_path() -> Path:
    env_path = os.environ.get("MODEL_PATH") or os.environ.get("MODELS_DIR")
    if env_path:
        p = Path(env_path)
        # If MODELS_DIR was provided, prefer a default filename
        if p.is_dir():
            return p.joinpath("phishing_model.pkl")
        return p
    # default fallback
    return Path("/app/models/phishing_model.pkl")

def get_model() -> Any:
    """
    Return the cached model, loading it if necessary.

    - Thread-safe and idempotent.
    - Raises exceptions on unrecoverable load errors so callers can handle/log them.
    """
    global _model, _model_path_cached
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        path = _resolve_model_path()
        _model_path_cached = str(path)
        if not path.exists():
            msg = f"preload_model: model file not found at {path}"
            logger.debug(msg)
            raise FileNotFoundError(msg)

        try:
            suffix = path.suffix.lower()
            if suffix in (".pkl", ".pickle"):
                _model = _load_with_pickle(path)
            elif suffix in (".joblib",):
                _model = _load_with_joblib(path)
            else:
                # Try pickle as a safe default
                try:
                    _model = _load_with_pickle(path)
                except Exception:
                    _model = _load_with_joblib(path)
            logger.info("preload_model: loaded model from %s", path)
            return _model
        except Exception as exc:
            logger.exception("preload_model: failed to load model from %s", path)
            # Clear any partial state
            _model = None
            raise

def get_model_path() -> Optional[str]:
    """
    Return the resolved model path used during the last load attempt, or None.
    """
    return _model_path_cached
