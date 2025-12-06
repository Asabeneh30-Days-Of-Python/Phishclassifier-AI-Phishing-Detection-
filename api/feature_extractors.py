# api/feature_extractors.py
import re
import numpy as np
import email
from collections import Counter
from datetime import datetime
import joblib   # type: ignore[reportMissingImports]
from sklearn.base import BaseEstimator, TransformerMixin    # type: ignore[reportMissingImports]
from typing import Any
import os
import json

# Project-root aware paths
PROJ_ROOT = os.getenv("PROJECT_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", os.path.join(PROJ_ROOT, "scripts"))
FEATURE_MAP_PATH = os.getenv("FEATURE_MAP_PATH", os.path.join(SCRIPTS_DIR, "feature_map.json"))

# Attempt to load feature map if present (non-fatal)
try:
    with open(FEATURE_MAP_PATH, "r", encoding="utf-8-sig") as _f:
        FEATURE_MAP = json.load(_f)
except Exception:
    FEATURE_MAP = {}

# VADER sentiment import (ignore editor warnings if not yet installed)
try:
    from vaderSentiment import SentimentIntensityAnalyzer   # type: ignore[reportMissingImports]
except Exception:
    # fallback import path used in some distributions
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # type: ignore
    except Exception:
        SentimentIntensityAnalyzer = None  # type: ignore

# Robust textstat import: handle different package layouts and provide a safe shim
try:
    import textstat  # type: ignore
    if not hasattr(textstat, "flesch_reading_ease"):
        try:
            from textstat.textstat import textstat as _ts  # type: ignore
            textstat = _ts  # type: ignore
        except Exception:
            pass
    if not hasattr(textstat, "flesch_reading_ease"):
        raise ImportError("textstat missing flesch_reading_ease attribute")
except Exception:
    class _DummyTextStat:
        @staticmethod
        def flesch_reading_ease(text: str) -> float:
            return 0.0
    textstat: Any = _DummyTextStat()

# instantiate once for performance (guard for missing package)
if SentimentIntensityAnalyzer is not None:
    try:
        SENTIMENT_ANALYZER = SentimentIntensityAnalyzer()
    except Exception:
        SENTIMENT_ANALYZER = None  # type: ignore
else:
    SENTIMENT_ANALYZER = None  # type: ignore


class UrgencyFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Extracts urgency features (binary flag and count) from email text.
    """
    def __init__(self):
        self.urgent_keywords = [
            'urgent', 'immediate', 'verify',
            'compromised', 'alert', 'attention',
            'action required'
        ]

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for text in X:
            txt = text if isinstance(text, str) else str(text)
            txt_lower = txt.lower()
            count = sum(txt_lower.count(w) for w in self.urgent_keywords)
            flag = 1 if count > 0 else 0
            rows.append([flag, count])
        return np.array(rows)


class URLFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Extracts a URL count feature from email text.
    """
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for text in X:
            txt = text if isinstance(text, str) else str(text)
            count = len(re.findall(r'http[s]?://', txt))
            rows.append([count])
        return np.array(rows)


class SentimentFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Extracts the VADER compound sentiment score from email text.
    If VADER not installed, returns 0.0.
    """
    def __init__(self):
        self.analyzer = SENTIMENT_ANALYZER

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for text in X:
            txt = text if isinstance(text, str) else str(text)
            try:
                score = self.analyzer.polarity_scores(txt)["compound"] if self.analyzer is not None else 0.0
            except Exception:
                score = 0.0
            rows.append([score])
        return np.array(rows)


class HeaderFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Extracts header-based features: SPF, DKIM, DMARC pass flags and count of Received headers.
    """
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for raw in X:
            txt = raw if isinstance(raw, str) else str(raw)
            try:
                msg = email.message_from_string(txt)
            except Exception:
                msg = email.message_from_string(str(raw))

            spf = int(msg.get("Received-SPF", "").lower().startswith("pass"))
            dkim = int("pass" in msg.get("DKIM-Signature", "").lower())
            dmarc = int("dmarc=pass" in msg.get("Authentication-Results", "").lower())
            received = len(msg.get_all("Received", []) or [])

            rows.append([spf, dkim, dmarc, received])
        return np.array(rows)


class AttachmentFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Counts attachments by file extension.
    """
    def __init__(self, exts=("exe", "scr", "zip", "pdf")):
        self.exts = exts

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for raw in X:
            txt = raw if isinstance(raw, str) else str(raw)
            try:
                msg = email.message_from_string(txt)
            except Exception:
                msg = email.message_from_string(str(raw))
            exts = []
            for part in msg.walk():
                fn = part.get_filename()
                if fn and "." in fn:
                    exts.append(fn.rsplit(".", 1)[1].lower())
            counter = Counter(exts)
            rows.append([counter.get(e, 0) for e in self.exts])
        return np.array(rows)


class EmbeddingFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Loads a Sentence-BERT model and returns embeddings for each text.
    Expects a joblib-serialised object with an .encode(text) method.
    """
    def __init__(self, model_path=None):
        model_path = model_path or os.getenv("SBERT_MODEL_PATH", os.path.join(PROJ_ROOT, "models", "sbert.pkl"))
        # joblib.load will raise if model missing; let caller handle it
        self.model = joblib.load(model_path)

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        embeddings = []
        for text in X:
            txt = text if isinstance(text, str) else str(text)
            embeddings.append(self.model.encode(txt))
        return np.vstack(embeddings)


class BehavioralFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Extracts behavioral signals: hour-of-day from Date header and readability score.
    """
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for raw in X:
            txt = raw if isinstance(raw, str) else str(raw)
            # parse hour from Date header
            hour = -1
            try:
                msg = email.message_from_string(txt)
                date_hdr = msg.get("Date", "")
                try:
                    dt = datetime.fromisoformat(date_hdr)
                    hour = dt.hour
                except Exception:
                    try:
                        from email.utils import parsedate_to_datetime
                        dt2 = parsedate_to_datetime(date_hdr)
                        hour = dt2.hour
                    except Exception:
                        hour = -1
            except Exception:
                hour = -1

            # readability via textstat shim or real library
            try:
                read = float(textstat.flesch_reading_ease(txt))
            except Exception:
                read = 0.0
            rows.append([hour, read])
        return np.array(rows)
