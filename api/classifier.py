# api/classifier.py

# api/classifier.py
import os
import json
import pickle
import logging
import joblib
import numpy as np
from typing import Any

# Import feature extraction and attacker insights
from enhanced_features import extract_features
from api.attacker_insights import analyze_attacker_motives
from api.intel import extract_urls  # lightweight url extraction for link features

# configure module-level logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Logger file generation update: write module logs to a rotating file in APP_LOG_DIR (best-effort)
LOG_DIR = os.environ.get("APP_LOG_DIR", "/app/logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    LOG_DIR = "/tmp"
CLASSIFIER_LOG_PATH = os.path.join(LOG_DIR, "classifier.log")

try:
    from logging.handlers import RotatingFileHandler

    _clf_handler = RotatingFileHandler(CLASSIFIER_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
    _clf_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s"))
    # Avoid adding multiple handlers if this module is imported more than once
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == getattr(_clf_handler, "baseFilename", None) for h in logger.handlers):
        logger.addHandler(_clf_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
except Exception:
    # If the file handler cannot be created, continue using default logging configuration
    try:
        logger.debug("classifier log file handler could not be created; falling back to basic logger")
    except Exception:
        pass

PROJ_ROOT = os.getenv("PROJECT_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
MODELS_DIR = os.getenv("MODELS_DIR", os.path.join(PROJ_ROOT, "models"))
SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", os.path.join(PROJ_ROOT, "scripts"))

MODEL_PATH = os.getenv("MODEL_PATH", os.path.join(MODELS_DIR, "phishing_model.pkl"))
THRESHOLDS_PATH = os.getenv("THRESHOLDS_PATH", os.path.join(SCRIPTS_DIR, "thresholds.json"))

THRESHOLD_BETA = os.getenv('THRESHOLD_BETA', '1.0')
THRESHOLD_MODE = os.getenv('THRESHOLD_MODE', 'WeightedGlobal')
DATASET_NAME = os.getenv('DATASET_NAME', '')

SURROGATE_PATH = os.getenv("SURROGATE_PATH", os.path.join(MODELS_DIR, "surrogate_explainer.joblib"))
_surrogate = None
_EXPLAINER_AVAILABLE = None  # None == unchecked, True/False after validation


def _load_thresholds(path: str, beta: str, mode: str, dataset_name: str) -> float:
    try:
        with open(path, "r", encoding="utf-8-sig") as tfp:
            _ALL_THRESH = json.load(tfp)
        _KEY = f"F{float(beta):.1f}"
        _TH_MAP = _ALL_THRESH.get(_KEY, {})
        if mode == 'PerDataset' and dataset_name:
            return float(_TH_MAP.get('PerDataset', {}).get(dataset_name, 0.7))
        return float(_TH_MAP.get(mode, 0.7))
    except Exception as e:
        logger.warning("Failed to load thresholds from %s (%s); defaulting to 0.7", path, e)
        return 0.7


MODEL_THRESHOLD = _load_thresholds(THRESHOLDS_PATH, THRESHOLD_BETA, THRESHOLD_MODE, DATASET_NAME)
logger.info("Using threshold: β=%s mode=%s → %s", THRESHOLD_BETA, THRESHOLD_MODE, MODEL_THRESHOLD)


def _load_model(path: str) -> Any:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Model file not found at {path}")
    try:
        return joblib.load(path)
    except Exception:
        with open(path, "rb") as f:
            return pickle.load(f)


try:
    MODEL = _load_model(MODEL_PATH)
except Exception as e:
    logger.exception("Failed to load model from %s: %s", MODEL_PATH, e)
    MODEL = None  # type: ignore

# expose a simple accessor for other modules/tests
def get_model() -> Any:
    return MODEL

MODEL_VERSION = getattr(MODEL, "version", None)


def email_to_features(email_text: str) -> dict:
    features = extract_features(email_text)
    # attach link-related features (counts and simple heuristics)
    urls = extract_urls(email_text)
    features["urls"] = urls
    features["num_urls"] = len(urls)
    # short-url heuristic: presence of common shorteners
    shorteners = ("bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly")
    features["has_short_url"] = any(any(s in u for s in shorteners) for u in urls)
    return features


def _simple_link_suspicion(url: str) -> dict:
    """
    Basic heuristic to compute a per-link suspicion score (0.0-1.0) and short reasons.
    Keep simple, deterministic, and safe (no remote calls here).
    """
    score = 0.0
    reasons = []

    if not url:
        return {"url": url, "suspicion": 0.0, "reasons": []}

    u = url.lower()
    # penalize shorteners moderately
    if any(s in u for s in ("bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly")):
        score += 0.25
        reasons.append("uses URL shortener")

    # IP address in hostname
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if host and all(ch.isdigit() or ch == "." for ch in host):
            score += 0.25
            reasons.append("numeric host (possible raw IP)")

        # suspicious TLDs heuristic
        suspicious_tlds = (".tk", ".pw", ".cf", ".gq")
        if any(host.endswith(t) for t in suspicious_tlds):
            score += 0.15
            reasons.append("suspicious top-level domain")
    except Exception:
        host = ""

    # long query strings / unusual length
    if len(url) > 200:
        score += 0.10
        reasons.append("very long URL")

    # final clamp and translate to percent later when used
    return {"url": url, "host": host, "suspicion": min(score, 1.0), "reasons": reasons}


def predict_single(email_text: str) -> np.ndarray:
    if MODEL is None:
        raise RuntimeError("MODEL is not loaded")
    features = email_to_features(email_text)
    cleaned_text = features.get("cleaned_text", email_text)
    return MODEL.predict([cleaned_text])


def predict_proba_single(email_text: str) -> np.ndarray:
    if MODEL is None:
        raise RuntimeError("MODEL is not loaded")
    features = email_to_features(email_text)
    cleaned_text = features.get("cleaned_text", email_text)
    return MODEL.predict_proba([cleaned_text])


def predict(email_text_or_list: Any):
    if isinstance(email_text_or_list, str):
        return predict_single(email_text_or_list)
    if isinstance(email_text_or_list, (list, tuple)):
        out = []
        for txt in email_text_or_list:
            res = predict_single(txt)
            try:
                if hasattr(res, "__len__") and len(res) == 1:
                    out.append(res[0])
                else:
                    out.append(res)
            except Exception:
                out.append(res)
        return out
    return predict_single(str(email_text_or_list))


def predict_proba(email_text_or_list: Any):
    if isinstance(email_text_or_list, str):
        return predict_proba_single(email_text_or_list)
    if isinstance(email_text_or_list, (list, tuple)):
        out = []
        for txt in email_text_or_list:
            p = predict_proba_single(txt)
            try:
                if hasattr(p, "__len__") and len(p) and hasattr(p[0], "__len__"):
                    out.append(list(p[0]))
                else:
                    out.append(p)
            except Exception:
                out.append(p)
        return out
    return predict_proba_single(str(email_text_or_list))


def _load_surrogate():
    global _surrogate
    if _surrogate is not None:
        return _surrogate
    if os.path.exists(SURROGATE_PATH):
        try:
            _surrogate = joblib.load(SURROGATE_PATH)
            logger.info("Loaded surrogate explainer from %s", SURROGATE_PATH)
            return _surrogate
        except Exception:
            logger.exception("Failed to load surrogate explainer from %s", SURROGATE_PATH)
    return None


def _get_phishing_coef_vector(clf) -> np.ndarray:
    source = clf
    for attr in ("base_estimator", "estimator", "_base_estimator", "_estimator"):
        if not hasattr(source, "coef_") and hasattr(source, attr):
            candidate = getattr(source, attr)
            if candidate is not None:
                source = candidate

    if hasattr(source, "coef_"):
        coef = np.array(source.coef_)
        if coef.ndim == 2:
            if coef.shape[0] == 1:
                return coef[0]
            classes = getattr(source, "classes_", None) or getattr(clf, "classes_", None)
            if classes is not None:
                try:
                    if 1 in list(classes):
                        idx = list(classes).index(1)
                    else:
                        idx = len(classes) - 1
                except Exception:
                    idx = coef.shape[0] - 1
            else:
                idx = coef.shape[0] - 1
            return coef[idx]
        else:
            return coef.ravel()
    raise AttributeError("Underlying estimator with coef_ not found")


def _locate_tfidf_position_in_union(features_union) -> tuple[int, int]:
    """
    Returns (start_index, n_features) for the tfidf slice within a FeatureUnion-like object.
    Raises ValueError if tfidf transformer cannot be located.
    """
    start = 0
    n = 0

    if hasattr(features_union, "transformer_list"):
        for name, trans in features_union.transformer_list:
            if name == "tfidf":
                try:
                    vec = trans.named_steps.get("tfidf") if hasattr(trans, "named_steps") else None
                    if vec is not None:
                        n = len(vec.get_feature_names_out())
                    elif hasattr(trans, "get_feature_names_out"):
                        n = len(trans.get_feature_names_out())
                except Exception:
                    n = 0
                return start, n
            try:
                vec = trans.named_steps.get("tfidf") if hasattr(trans, "named_steps") else None
                if vec is not None:
                    start += len(vec.get_feature_names_out())
                else:
                    try:
                        size = trans.transform([""]).shape[1]
                        start += size
                    except Exception:
                        pass
            except Exception:
                pass

    if hasattr(features_union, "transformers"):
        for name, trans, _ in features_union.transformers:
            if name == "tfidf":
                try:
                    vec = trans.named_steps.get("tfidf") if hasattr(trans, "named_steps") else None
                    if vec is not None:
                        n = len(vec.get_feature_names_out())
                    elif hasattr(trans, "get_feature_names_out"):
                        n = len(trans.get_feature_names_out())
                except Exception:
                    n = 0
                return start, n
            try:
                vec = trans.named_steps.get("tfidf") if hasattr(trans, "named_steps") else None
                if vec is not None:
                    start += len(vec.get_feature_names_out())
                else:
                    try:
                        size = trans.transform([""]).shape[1]
                        start += size
                    except Exception:
                        pass
            except Exception:
                pass

    raise ValueError("tfidf transformer not found in provided features union")


# --- Explainer validation and safe SHAP wrapper -----------------------------
def _feature_count_of_model(clf) -> int | None:
    try:
        coef = _get_phishing_coef_vector(clf)
        return int(coef.shape[0])
    except Exception:
        try:
            return int(getattr(clf, "n_features_in_", None) or getattr(clf, "n_features_", None))
        except Exception:
            return None


def _feature_count_of_vectorizer(vec) -> int | None:
    try:
        return int(len(vec.get_feature_names_out()))
    except Exception:
        try:
            vocab = getattr(vec, "vocabulary_", None)
            return int(len(vocab)) if isinstance(vocab, dict) else None
        except Exception:
            try:
                s = vec.transform([""])
                return int(s.shape[1])
            except Exception:
                return None


def _validate_explainer_availability(features_union, vectorizer):
    global _EXPLAINER_AVAILABLE
    if _EXPLAINER_AVAILABLE is not None:
        return _EXPLAINER_AVAILABLE
    try:
        model_cols = _feature_count_of_model(MODEL)
        vectorizer_cols = _feature_count_of_vectorizer(vectorizer)
        logger.info("Artifact shapes check: model_cols=%s vectorizer_cols=%s", model_cols, vectorizer_cols)
        if model_cols is None or vectorizer_cols is None:
            _EXPLAINER_AVAILABLE = False
        else:
            _EXPLAINER_AVAILABLE = (int(model_cols) == int(vectorizer_cols))
            if not _EXPLAINER_AVAILABLE:
                logger.error("Model/Vectorizer mismatch: model=%s vectorizer=%s; disabling explainer", model_cols, vectorizer_cols)
    except Exception as e:
        logger.exception("Explainer validation failed: %s", e)
        _EXPLAINER_AVAILABLE = False
    return _EXPLAINER_AVAILABLE


def _safe_explain(clf_calibrated, vectorizer, cleaned_text, feature_names, nonzero_indices, top_n=5, used_explainer=None):
    """
    Attempt SHAP explanations safely. If artifacts mismatch, shap missing, or runtime fails,
    return a deterministic fallback explanation list so callers do not crash.
    """
    try:
        features_union = MODEL.named_steps.get("features") if hasattr(MODEL, "named_steps") else None
    except Exception:
        features_union = None

    if not _validate_explainer_availability(features_union, vectorizer):
        logger.warning("Explainer unavailable due to artifact mismatch; returning fallback explanations.")
        return [
            {"feature": feature_names[idx], "contribution": None, "interpretation": "explanation unavailable (explainer disabled)", "explainer": used_explainer or "none"}
            for idx in list(nonzero_indices)[:top_n]
        ]

    try:
        import shap  # type: ignore[reportMissingImports]
    except Exception:
        logger.warning("shap import failed; returning sentinel explanations")
        return [
            {"feature": feature_names[idx], "contribution": None, "interpretation": "explanation unavailable (shap missing)", "explainer": used_explainer or "none"}
            for idx in list(nonzero_indices)[:top_n]
        ]

    try:
        instance_vec = vectorizer.transform([cleaned_text])
        try:
            background = np.zeros((1, instance_vec.shape[1]))
        except Exception:
            background = np.zeros((1, len(feature_names)))

        model_for_explainer = clf_calibrated
        model_name = type(model_for_explainer).__name__.lower()
        if "tree" in model_name or "forest" in model_name or "xgboost" in model_name or "lgbm" in model_name:
            explainer = shap.TreeExplainer(model_for_explainer, data=background)
        else:
            predict_fn = lambda x: np.array(model_for_explainer.predict_proba(x))[:, 1]
            explainer = shap.KernelExplainer(predict_fn, background)

        try:
            shap_values = explainer.shap_values(instance_vec, check_additivity=False)
            if isinstance(shap_values, list):
                sv = shap_values[-1]
            else:
                sv = shap_values
            sv = np.array(sv).reshape(-1)
        except Exception as e:
            logger.warning("SHAP runtime failed: %s", e)
            return [
                {"feature": feature_names[idx], "contribution": None, "interpretation": "explanation unavailable (shap runtime failure)", "explainer": used_explainer or "shap"}
                for idx in list(nonzero_indices)[:top_n]
            ]

        feature_contribs = []
        for idx in nonzero_indices:
            feat = feature_names[idx]
            contrib = float(sv[idx]) if idx < sv.shape[0] else None
            reason = "increases phishing risk" if contrib is not None and contrib > 0 else "decreases phishing risk"
            feature_contribs.append((feat, contrib, reason))
        feature_contribs.sort(key=lambda x: abs(x[1]) if x[1] is not None else 0.0, reverse=True)
        return [
            {"feature": feat, "contribution": (round(float(contrib), 6) if contrib is not None else None), "interpretation": reason, "explainer": used_explainer or "shap"}
            for feat, contrib, reason in feature_contribs[:top_n]
        ]
    except Exception as e:
        logger.exception("safe_explain unexpected failure: %s", e)
        return [
            {"feature": feature_names[idx], "contribution": None, "interpretation": "explanation unavailable", "explainer": used_explainer or "none"}
            for idx in list(nonzero_indices)[:top_n]
        ]


# ---------------------------------------------------------------------------
def explain_prediction(email_text: str, top_n: int = 5) -> list[dict]:
    if MODEL is None:
        raise RuntimeError("MODEL is not loaded")

    features = email_to_features(email_text)
    cleaned_text = features.get("cleaned_text", email_text)

    try:
        clf_calibrated = MODEL.named_steps.get("clf", MODEL) if hasattr(MODEL, "named_steps") else MODEL
        features_union = MODEL.named_steps.get("features") if hasattr(MODEL, "named_steps") else None
    except Exception as e:
        raise RuntimeError(f"Model pipeline layout not as expected: {e}")

    if features_union is None:
        raise ValueError("Model pipeline does not expose a 'features' union/transformer; cannot explain.")

    tfidf_pipeline = None
    # First try named transformer 'tfidf'
    if hasattr(features_union, "transformer_list"):
        for name, trans in features_union.transformer_list:
            if name == "tfidf":
                tfidf_pipeline = trans
                break
    elif hasattr(features_union, "transformers"):
        for name, trans, _ in features_union.transformers:
            if name == "tfidf":
                tfidf_pipeline = trans
                break

    # Fallback: locate a candidate vectorizer if 'tfidf' name isn't present
    if tfidf_pipeline is None:
        try:
            # search transformer_list for first transformer with a vectorizer-like step
            if hasattr(features_union, "transformer_list"):
                for name, trans in features_union.transformer_list:
                    candidate = None
                    try:
                        candidate = trans.named_steps.get("tfidf") if hasattr(trans, "named_steps") else None
                    except Exception:
                        candidate = None
                    if candidate is not None or hasattr(trans, "get_feature_names_out") or hasattr(trans, "vocabulary_"):
                        tfidf_pipeline = trans
                        break
            elif hasattr(features_union, "transformers"):
                for name, trans, _ in features_union.transformers:
                    candidate = None
                    try:
                        candidate = trans.named_steps.get("tfidf") if hasattr(trans, "named_steps") else None
                    except Exception:
                        candidate = None
                    if candidate is not None or hasattr(trans, "get_feature_names_out") or hasattr(trans, "vocabulary_"):
                        tfidf_pipeline = trans
                        break
        except Exception:
            tfidf_pipeline = None

    if tfidf_pipeline is None:
        # provide clearer message and delegate to safe explainer that doesn't require TFIDF alignment
        raise ValueError("Tfidf pipeline not found under key 'tfidf' and no fallback vectorizer detected.")

    # Obtain vectorizer step; be tolerant if step name differs
    vectorizer = None
    try:
        if hasattr(tfidf_pipeline, "named_steps"):
            if "tfidf" in tfidf_pipeline.named_steps:
                vectorizer = tfidf_pipeline.named_steps["tfidf"]
            else:
                # pick first named step that looks like vectorizer
                for step_name, step in tfidf_pipeline.named_steps.items():
                    if hasattr(step, "get_feature_names_out") or hasattr(step, "vocabulary_"):
                        vectorizer = step
                        break
        else:
            # transformer's own interface
            if hasattr(tfidf_pipeline, "get_feature_names_out") or hasattr(tfidf_pipeline, "vocabulary_"):
                vectorizer = tfidf_pipeline
    except Exception:
        vectorizer = None

    if vectorizer is None:
        raise ValueError("Tfidf vectorizer not found in tfidf pipeline; cannot explain.")

    feature_names = vectorizer.get_feature_names_out()
    X = vectorizer.transform([cleaned_text]).toarray()[0]
    nonzero_indices = np.where(X > 0)[0]

    # Surrogate preference
    if os.getenv("SURROGATE_PREFERRED", "") in ("1", "true", "True", "yes", "on"):
        surrogate = _load_surrogate()
        if surrogate and surrogate.get("coef") is not None:
            coef_vec = np.array(surrogate["coef"], dtype=float)
            used_explainer = "surrogate"
            try:
                try:
                    start_idx, n_tfidf = _locate_tfidf_position_in_union(features_union)
                except Exception:
                    start_idx, n_tfidf = 0, len(feature_names)
                if n_tfidf == 0:
                    n_tfidf = len(feature_names)

                if start_idx + n_tfidf > coef_vec.shape[0]:
                    logger.warning("Coefficient vector shorter than expected for tfidf slice; using first n features")
                    coeffs_tfidf = coef_vec[:n_tfidf]
                else:
                    coeffs_tfidf = coef_vec[start_idx:start_idx + n_tfidf]

                contributions = X * coeffs_tfidf
                feature_contribs = []
                for idx in nonzero_indices:
                    feat = feature_names[idx]
                    contrib = contributions[idx]
                    reason = "increases phishing risk" if contrib > 0 else "decreases phishing risk"
                    feature_contribs.append((feat, contrib, reason))
                feature_contribs.sort(key=lambda x: abs(x[1]), reverse=True)
                return [
                    {"feature": feat, "contribution": round(float(contrib), 4), "interpretation": reason, "explainer": used_explainer}
                    for feat, contrib, reason in feature_contribs[:top_n]
                ]
            except Exception:
                logger.exception("Surrogate explanation attempt failed; falling back to normal path")

    coef_vec = None
    used_explainer = None
    try:
        coef_vec = _get_phishing_coef_vector(clf_calibrated)
        used_explainer = "native"
    except Exception:
        surrogate = _load_surrogate()
        if surrogate and surrogate.get("coef") is not None:
            coef_vec = np.array(surrogate["coef"], dtype=float)
            used_explainer = "surrogate"
        else:
            coef_vec = None
            used_explainer = None

    if coef_vec is not None:
        try:
            try:
                start_idx, n_tfidf = _locate_tfidf_position_in_union(features_union)
            except Exception:
                start_idx, n_tfidf = 0, len(feature_names)
            if n_tfidf == 0:
                n_tfidf = len(feature_names)

            if start_idx + n_tfidf > coef_vec.shape[0]:
                logger.warning(
                    "Coefficient vector shorter than expected for tfidf slice (start %d len %d coef_len %d); using first n features",
                    start_idx, n_tfidf, coef_vec.shape[0]
                )
                coeffs_tfidf = coef_vec[:n_tfidf]
            else:
                coeffs_tfidf = coef_vec[start_idx:start_idx + n_tfidf]

            contributions = X * coeffs_tfidf
            feature_contribs = []
            for idx in nonzero_indices:
                feat = feature_names[idx]
                contrib = contributions[idx]
                reason = "increases phishing risk" if contrib > 0 else "decreases phishing risk"
                feature_contribs.append((feat, contrib, reason))
            feature_contribs.sort(key=lambda x: abs(x[1]), reverse=True)
            return [
                {"feature": feat, "contribution": round(float(contrib), 4), "interpretation": reason, "explainer": used_explainer}
                for feat, contrib, reason in feature_contribs[:top_n]
            ]
        except Exception as e:
            logger.warning("Linear-coef explanation failed: %s", e)

    # Delegate complex / shap-based explanation to safe wrapper that validates artifacts and guards runtime.
    return _safe_explain(clf_calibrated, vectorizer, cleaned_text, feature_names, nonzero_indices, top_n, used_explainer)


def classify_email(
    email_text: str,
    threshold: float | None = None,
    extra_features: dict | None = None,
    threat_score: float | None = None
) -> dict:
    if threshold is None:
        threshold = MODEL_THRESHOLD

    features = extra_features if extra_features is not None else email_to_features(email_text)
    threat_score = threat_score if threat_score is not None else features.get("threat_score", 0.5)

    probabilities = predict_proba(email_text)
    try:
        if isinstance(probabilities, (list, tuple)) and len(probabilities) and hasattr(probabilities[0], "__len__") and len(probabilities[0]) > 1:
            prob = float(probabilities[0][1])
        elif isinstance(probabilities, (list, tuple)) and len(probabilities):
            prob = float(probabilities[0])
        else:
            prob = float(np.array(probabilities).reshape(-1)[-1])
    except Exception:
        prob = 0.0

    risk_score = float(prob)
    # canonical lowercase prediction used by routes and UI mapping
    prediction = "phishing" if risk_score >= threshold else "legitimate"
    prediction_display = "Phishing" if risk_score >= threshold else "Legitimate"

    risk_score_percent = round(risk_score * 100.0, 1)
    risk_score_display = f"{risk_score_percent}%"
    threshold_percent = round(threshold * 100.0, 1)
    threshold_display = f"{threshold_percent}%"

    technical_explanation = explain_prediction(email_text, top_n=5)

    # attacker insights: call domain-level analyzer and enrich motives/recommendations with link context
    attacker_insights = analyze_attacker_motives(email_text) if 'analyze_attacker_motives' in globals() else {}
    # build link_analysis array
    urls = features.get("urls", [])
    link_analysis = []
    for u in urls:
        la = _simple_link_suspicion(u)
        # convert suspicion 0-1 to percent 0-100 and include short textual reasons
        la["suspicion_percent"] = round(la.get("suspicion", 0.0) * 100.0, 1)
        link_analysis.append(la)

    # add link-level summaries into attacker_insights for UI convenience
    if link_analysis:
        attacker_insights = dict(attacker_insights)  # shallow copy if provided
        attacker_insights.setdefault("link_analysis", link_analysis)
        # surface a short summary array of top links by suspicion
        sorted_links = sorted(link_analysis, key=lambda x: x.get("suspicion", 0), reverse=True)[:3]
        attacker_insights.setdefault("top_links", [{"host": l.get("host", ""), "suspicion_percent": l.get("suspicion_percent", 0.0)} for l in sorted_links])

    logger.debug("classify_email: risk_score=%.3f threshold=%.3f → %s", risk_score, threshold, prediction)

    return {
        "prediction":            prediction,
        "prediction_display":    prediction_display,
        "probability":           prob,
        "risk_score":            risk_score,
        "risk_score_percent":    risk_score_percent,
        "risk_score_display":    risk_score_display,
        "threshold":             threshold,
        "threshold_percent":     threshold_percent,
        "threshold_display":     threshold_display,
        "technical_explanation": technical_explanation,
        "attacker_insights":     attacker_insights,
        "link_analysis":         link_analysis,
        "model_version":         MODEL_VERSION
    }


def classify_text(email_text: str) -> dict:
    return classify_email(email_text)


class _ClassifierAdapter:
    def predict(self, X):
        if isinstance(X, str):
            X = [X]
        return predict(list(X))

    def predict_proba(self, X):
        if isinstance(X, str):
            X = [X]
        return predict_proba(list(X))


classifier = _ClassifierAdapter()

__all__ = [
    "predict",
    "predict_proba",
    "predict_single",
    "predict_proba_single",
    "classify_text",
    "classify_email",
    "explain_prediction",
    "classifier",
    "get_model",
    "MODEL_VERSION",
]
