# train_model.py

import os
import re
import pickle
import argparse
import logging
import time
from typing import List, Optional, Tuple, Callable, Dict

from api.datasets import (
    load_sample_dataset,
    load_enron_dataset,
    load_spamassassin_dataset,
    load_phishtank_dataset,
    load_hybrid_enterprise_dataset,
)
from api.feature_extractors import (
    UrgencyFeatureExtractor,
    URLFeatureExtractor,
    SentimentFeatureExtractor,
    HeaderFeatureExtractor,
    AttachmentFeatureExtractor,
    BehavioralFeatureExtractor,
    EmbeddingFeatureExtractor,
)

# Optional libs (shap) are imported lazily below with fallbacks
BASE_DIR = os.path.dirname(__file__)
MODEL_DIR = os.path.join(BASE_DIR, "models")
MODEL_FILE = os.path.join(MODEL_DIR, "phishing_model.pkl")
EXPLAIN_DIR = os.path.join(MODEL_DIR, "explanations")

ENRON_SAMPLE_LIMIT = int(os.getenv("ENRON_SAMPLE_LIMIT", "50000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(message)s"
)

DATASET_LOADERS: Dict[str, Callable[..., Tuple[List[str], List[int]]]] = {
    "sample":    load_sample_dataset,
    "enron":     load_enron_dataset,
    "spamassn":  load_spamassassin_dataset,
    "phishtank": load_phishtank_dataset,
    "enterprise": load_hybrid_enterprise_dataset,
}


def enrich_email_text(email_text: str) -> str:
    urgent_keywords = [
        "urgent", "immediate", "verify",
        "compromised", "alert", "action required"
    ]
    text_lower = email_text.lower()
    urgent_count = sum(text_lower.count(w) for w in urgent_keywords)
    url_count = len(re.findall(r"http[s]?://", email_text))
    threat_rep = 0.5
    return (
        f"{email_text} "
        f"FEATURE_URGENT_COUNT:{urgent_count} "
        f"FEATURE_URL_COUNT:{url_count} "
        f"THREAT_REP:{threat_rep}"
    )


def _build_embedding_branch():
    model_path = os.path.join(MODEL_DIR, "sbert.pkl")
    if os.path.exists(model_path):
        return ("embedding", EmbeddingFeatureExtractor(model_path=model_path))
    return None


def build_standard_pipeline(include_embedding: bool = True):
    from sklearn.pipeline import Pipeline, FeatureUnion
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.calibration import CalibratedClassifierCV

    tfidf_branch = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 3), stop_words="english"))
    ])

    feature_list = [
        ("tfidf", tfidf_branch),
        ("urgency", UrgencyFeatureExtractor()),
        ("url", URLFeatureExtractor()),
        ("sentiment", SentimentFeatureExtractor()),
        ("header", HeaderFeatureExtractor()),
        ("attachment", AttachmentFeatureExtractor()),
        ("behavioral", BehavioralFeatureExtractor()),
    ]

    if include_embedding:
        emb = _build_embedding_branch()
        if emb is not None:
            feature_list.append(emb)

    combined = FeatureUnion(feature_list)

    base_clf = LogisticRegression(
        solver="liblinear",
        random_state=42,
        class_weight="balanced",
        max_iter=200
    )
    calibrated = CalibratedClassifierCV(estimator=base_clf, cv=5)
    return Pipeline([("features", combined), ("clf", calibrated)])


def build_enron_pipeline():
    from sklearn.pipeline import Pipeline, FeatureUnion
    from sklearn.feature_extraction.text import HashingVectorizer
    from sklearn.linear_model import SGDClassifier

    hash_branch = Pipeline([
        ("hash", HashingVectorizer(
            n_features=2**18,
            alternate_sign=False,
            stop_words="english",
            ngram_range=(1, 2)
        ))
    ])

    combined = FeatureUnion([
        ("hash", hash_branch),
        ("urgency", UrgencyFeatureExtractor()),
        ("url", URLFeatureExtractor()),
        ("sentiment", SentimentFeatureExtractor()),
        ("header", HeaderFeatureExtractor()),
        ("attachment", AttachmentFeatureExtractor()),
        ("behavioral", BehavioralFeatureExtractor()),
    ])

    sgd = SGDClassifier(
         loss="log_loss",
         penalty="l2",
         max_iter=1,
         warm_start=True,
         random_state=42
     )
    return Pipeline([("features", combined), ("clf", sgd)])


def _try_build_stacked_classifier(base_pipeline):
    try:
        from sklearn.ensemble import StackingClassifier
        from sklearn.linear_model import LogisticRegression
        from lightgbm import LGBMClassifier

        estimators = [
            ("lr_base", base_pipeline),
            ("lgbm", LGBMClassifier(n_estimators=100, random_state=42))
        ]
        stack = StackingClassifier(
            estimators=estimators,
            final_estimator=LogisticRegression()
        )
        return stack
    except Exception:
        logging.info("LightGBM or stacking components not available; skipping stacking.")
        return base_pipeline


def _save_model(pipeline, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump(pipeline, f)


def _maybe_compute_shap(pipeline, X_sample, out_dir=EXPLAIN_DIR, max_samples=200):
    """
    Compute SHAP explanations for a sample subset if shap is installed.
    Saves numpy arrays and a small JSON summary for quick inspection.
    """
    try:
        import shap
        import numpy as np
    except Exception:
        logging.info("SHAP not available; skipping explainability step.")
        return

    os.makedirs(out_dir, exist_ok=True)
    # choose a lightweight explainer depending on model type
    try:
        clf = pipeline.named_steps.get("clf", pipeline)
        # if classifier is calibrator, use base_estimator for speed
        if hasattr(clf, "base_estimator"):
            model_for_explain = clf.base_estimator
        else:
            model_for_explain = clf

        # transform texts into feature matrix (avoid huge sample)
        X_trans = pipeline.named_steps["features"].transform(X_sample[:max_samples])
        # select explainer type
        explainer = shap.Explainer(model_for_explain.predict_proba, X_trans)
        shap_values = explainer(X_trans)
        # Persist shap values (binary) and a small mapping
        np.save(os.path.join(out_dir, "shap_values.npy"), shap_values.values)
        # Save feature names if available
        try:
            feature_names = []
            # attempt to get feature names from vectorizer and engineered extractors
            if hasattr(pipeline.named_steps["features"], "transformer_list"):
                for name, trans in pipeline.named_steps["features"].transformer_list:
                    feature_names.append(name)
        except Exception:
            feature_names = []
        with open(os.path.join(out_dir, "meta.txt"), "w") as fh:
            fh.write(f"n_samples={len(X_sample[:max_samples])}\n")
            fh.write(f"feature_buckets={','.join(feature_names)}\n")
        logging.info("SHAP explanations computed and saved to %s", out_dir)
    except Exception as exc:
        logging.exception("Error during SHAP computation: %s", exc)


def train_and_save_model(
    datasets: Optional[List[str]] = None,
    output_path: Optional[str] = None,
    use_stacking: bool = False,
) -> str:
    if not datasets:
        datasets = ["sample"]

    total_start = time.time()

    t0 = time.time()
    logging.info("TRAIN_STEP 1: loading data for %s", datasets)

    all_texts: List[str] = []
    all_labels: List[int] = []

    for ds in datasets:
        loader = DATASET_LOADERS.get(ds)
        if not loader:
            logging.warning("Unknown dataset '%s', skipping.", ds)
            continue

        if ds == "enron":
            texts, labels = loader(limit=ENRON_SAMPLE_LIMIT)
        else:
            texts, labels = loader()

        all_texts.extend(texts)
        all_labels.extend(labels)

    logging.info("STEP 1 done in %.1fs", time.time() - t0)

    t1 = time.time()
    logging.info("TRAIN_STEP 2: enriching and filtering data")
    enriched = [enrich_email_text(t) for t in all_texts]
    paired = [(t, l) for t, l in zip(enriched, all_labels) if t.strip()]
    if not paired:
        raise ValueError("No valid documents to train on; empty vocabulary.")
    texts, labels = zip(*paired)

    if len(set(labels)) < 2:
        sample_texts, sample_labels = load_sample_dataset()
        logging.warning(
            "Only one class detected %s; merging sample dataset",
            set(labels)
        )
        texts = list(texts) + sample_texts
        labels = list(labels) + sample_labels

    logging.info("STEP 2 done in %.1fs", time.time() - t1)

    is_enron_only = (datasets == ["enron"])
    t2 = time.time()

    if is_enron_only:
        logging.info("USING Enron-optimized incremental pipeline")
        pipeline = build_enron_pipeline()
        classes = list(set(labels))
        chunk_size = 50000

        for i in range(0, len(texts), chunk_size):
            sub_x = texts[i: i + chunk_size]
            sub_y = labels[i: i + chunk_size]
            if i == 0:
                X_chunk = pipeline.named_steps["features"].fit_transform(sub_x)
            else:
                X_chunk = pipeline.named_steps["features"].transform(sub_x)
            pipeline.named_steps["clf"].partial_fit(
                X_chunk, sub_y, classes=classes
            )
            logging.info(
                "  – partial_fit %d–%d done (%.1fs elapsed)",
                i, i + len(sub_x), time.time() - t2
            )

        logging.info("STEP 3 (Enron incremental) done in %.1fs", time.time() - t2)

    else:
        logging.info("USING standard batch pipeline")
        pipeline = build_standard_pipeline()
        if use_stacking:
            pipeline = _try_build_stacked_classifier(pipeline)
        pipeline.fit(texts, labels)
        logging.info("STEP 3 (standard batch) done in %.1fs", time.time() - t2)

    out_path = output_path or MODEL_FILE
    t3 = time.time()
    logging.info("TRAIN_STEP 4: saving model to %s", out_path)
    _save_model(pipeline, out_path)
    logging.info("STEP 4 done in %.1fs", time.time() - t3)

    # Compute SHAP explanations for a small sample if available
    try:
        sample_for_shap = list(texts)[:500]
        _maybe_compute_shap(pipeline, sample_for_shap)
    except Exception:
        logging.exception("SHAP computation failed")

    logging.info(
        "TRAIN_FINISHED total=%.1fs for datasets %s",
        time.time() - total_start,
        datasets
    )
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Train the PhishClassifier model")
    parser.add_argument(
        "-d", "--datasets",
        nargs="+",
        choices=list(DATASET_LOADERS.keys()),
        help="Datasets to include (default: sample)"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output path for the pickled model"
    )
    parser.add_argument(
        "--stack",
        action="store_true",
        help="Try stacking base pipeline with LightGBM (if available)"
    )
    args = parser.parse_args()
    train_and_save_model(datasets=args.datasets, output_path=args.output, use_stacking=args.stack)


def load_data(datasets: List[str]) -> Tuple[List[str], List[int]]:
    all_texts: List[str] = []
    all_labels: List[int] = []

    for ds in datasets:
        loader = DATASET_LOADERS.get(ds)
        if loader is None:
            logging.warning("load_data: unknown dataset '%s', skipping", ds)
            continue

        t0 = time.time()
        logging.info("load_data: loading %s…", ds)

        if ds == "enron":
            texts, labels = loader(limit=ENRON_SAMPLE_LIMIT)
        else:
            texts, labels = loader()
        logging.info("load_data: %s loaded in %.1fs", ds, time.time() - t0)

        all_texts.extend(texts)
        all_labels.extend(labels)

    return all_texts, all_labels


if __name__ == "__main__":
    main()
