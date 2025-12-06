# tools/build_surrogate_from_pipeline.py
import os
import sys
import joblib
import logging
import numpy as np
import scipy.sparse
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

# Ensure project root is importable when running as a module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.classifier import _load_model, MODEL_PATH

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "surrogate_explainer.joblib")


def load_texts_and_labels():
    """
    Replace with your real dataset loader.
    Fallback behavior:
      - if data/texts.txt and data/labels.txt exist, load them
      - otherwise use a small sample and attempt to pseudo-label with the production pipeline
    Returns:
      texts: list[str]
      labels: list[int] (1==phish, 0==legit)
    """
    txt = os.path.join(os.path.dirname(__file__), "..", "data", "texts.txt")
    lab = os.path.join(os.path.dirname(__file__), "..", "data", "labels.txt")
    if os.path.exists(txt) and os.path.exists(lab):
        with open(txt, encoding="utf-8") as f:
            texts = [l.strip() for l in f if l.strip()]
        with open(lab, encoding="utf-8") as f:
            labels = [int(l.strip()) for l in f if l.strip()]
        return texts, labels

    # small textual fallback
    sample_texts = [
        "your account under attack",
        "monthly newsletter",
        "reset your password now",
        "meeting agenda"
    ]

    # try to pseudo-label using the production pipeline
    try:
        pipe = _load_model(MODEL_PATH)
        preds = pipe.predict(sample_texts)
        labels = [1 if (p == "Phishing" or str(p) in ("1", "True", "true")) else 0 for p in preds]
        logger.info("Pseudo-labelled sample_texts using pipeline: %s", labels)
    except Exception as e:
        logger.warning("Could not pseudo-label sample_texts with pipeline: %s", e)
        labels = [1, 0, 1, 0]
    return sample_texts, labels


def find_tfidf_vectorizer(pipeline):
    """
    Locate the TF-IDF vectorizer inside the pipeline's feature union.
    """
    features_union = pipeline.named_steps.get("features") if hasattr(pipeline, "named_steps") else None
    if features_union is None:
        raise RuntimeError("pipeline has no 'features' union")
    # sklearn ColumnTransformer style
    if hasattr(features_union, "transformer_list"):
        for name, trans in features_union.transformer_list:
            if name == "tfidf":
                return trans.named_steps["tfidf"]
    # older/alternate style
    if hasattr(features_union, "transformers"):
        for name, trans, _ in features_union.transformers:
            if name == "tfidf":
                return trans.named_steps["tfidf"]
    raise RuntimeError("tfidf transformer not found")


def main():
    texts, labels = load_texts_and_labels()
    logger.info("Loaded %d texts for surrogate training", len(texts))

    pipe = _load_model(MODEL_PATH)
    tfidf = find_tfidf_vectorizer(pipe)
    X = tfidf.transform(texts)
    y = np.array(labels)

    # split; stratify if possible
    try:
        X_train, _, y_train, _ = train_test_split(
            X, y, test_size=0.2, random_state=42,
            stratify=y if len(np.unique(y)) > 1 else None
        )
    except Exception:
        # fallback if stratify failed
        X_train, _, y_train, _ = train_test_split(X, y, test_size=0.2, random_state=42)

    # Ensure at least two classes are present; attempt to augment using pseudo-labels
    unique_labels = np.unique(y_train)
    if len(unique_labels) < 2:
        logger.warning("Only one class present in training labels: %s", unique_labels.tolist())
        try:
            logger.info("Attempting to augment training set with additional pseudo-labeled examples from pipeline")
            more_texts = [
                "your account under attack",
                "reset your password immediately",
                "invoice attached",
                "team meeting minutes",
                "please verify your password",
                "security alert: suspicious login"
            ]
            more_preds = pipe.predict(more_texts)
            more_labels = np.array([1 if (p == "Phishing" or str(p) in ("1", "True", "true")) else 0 for p in more_preds])
            X_more = tfidf.transform(more_texts)
            X_train = scipy.sparse.vstack([X_train, X_more])
            y_train = np.concatenate([y_train, more_labels])
            unique_labels = np.unique(y_train)
            logger.info("After augmentation, unique labels: %s", unique_labels.tolist())
        except Exception as e:
            logger.warning("Pseudo-label augmentation failed: %s", e)

    # If still single-class, save a placeholder explainer and exit cleanly
    if len(unique_labels) < 2:
        logger.error("Insufficient class variety to train surrogate (still single-class). Saving placeholder explainer.")
        explainer = {
            "vocab_size": len(tfidf.get_feature_names_out()),
            "coef": None,
            "intercept": 0.0
        }
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        joblib.dump(explainer, OUT_PATH)
        logger.info("Saved placeholder surrogate to %s", OUT_PATH)
        return

    # Train surrogate logistic regression
    clf = LogisticRegression(
        penalty="l2",
        C=1.0,
        solver="saga",
        max_iter=1000,
        class_weight="balanced",
        n_jobs=-1,
        random_state=42
    )
    logger.info("Training surrogate logistic regression on %d examples", X_train.shape[0])
    clf.fit(X_train, y_train)

    explainer = {
        "vocab_size": len(tfidf.get_feature_names_out()),
        "coef": clf.coef_.ravel().astype("float32"),
        "intercept": float(clf.intercept_.ravel()[0]) if getattr(clf, "intercept_", None) is not None else 0.0
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    joblib.dump(explainer, OUT_PATH)
    logger.info("Saved surrogate explainer to %s", OUT_PATH)


if __name__ == "__main__":
    main()
