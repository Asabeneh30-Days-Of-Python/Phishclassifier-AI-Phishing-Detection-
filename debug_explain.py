from api.classifier import _load_model, MODEL_PATH, MODEL, _locate_tfidf_position_in_union, _get_phishing_coef_vector
import json

pipe = MODEL if MODEL is not None else _load_model(MODEL_PATH)
print("pipeline type:", type(pipe))

features_union = pipe.named_steps.get("features") if hasattr(pipe, "named_steps") else None
try:
    start_idx, n_tfidf = _locate_tfidf_position_in_union(features_union) if features_union is not None else (0, None)
except Exception as e:
    start_idx, n_tfidf = 0, None
print("tfidf start_idx:", start_idx, "n_tfidf:", n_tfidf)

tfidf_pipeline = None
names = []
idxs = []
try:
    if features_union is not None and hasattr(features_union, "transformer_list"):
        for name, trans in features_union.transformer_list:
            if name == "tfidf":
                tfidf_pipeline = trans
                break
    if tfidf_pipeline is None and features_union is not None and hasattr(features_union, "transformers"):
        for name, trans, _ in features_union.transformers:
            if name == "tfidf":
                tfidf_pipeline = trans
                break
    if tfidf_pipeline is not None:
        vectorizer = tfidf_pipeline.named_steps["tfidf"]
        names = list(vectorizer.get_feature_names_out())
        idxs = [i for i,n in enumerate(names) if n == "attack"]
        print("total tfidf features:", len(names))
        print("'attack' indices in vectorizer:", idxs)
except Exception as e:
    print("vectorizer access failed:", e)

try:
    clf = pipe.named_steps.get("clf", pipe) if hasattr(pipe, "named_steps") else pipe
    coef_vec = _get_phishing_coef_vector(clf)
    print("coef length:", coef_vec.shape[0])
    if n_tfidf is None and names:
        n_tfidf = len(names)
    if n_tfidf:
        slice_start = start_idx
        slice_end = start_idx + n_tfidf
        print("tfidf coefficients slice [start:end]:", slice_start, slice_end)
        tfidf_coefs = coef_vec[slice_start:slice_end]
        if idxs:
            for i in idxs:
                print(f"coef for 'attack' at tfidf index {i}:", float(tfidf_coefs[i]))
        else:
            print("No 'attack' token found in tfidf feature names.")
    else:
        print("n_tfidf unknown; first 20 coefs preview:", coef_vec[:20].tolist())
except Exception as e:
    print("coef extraction failed:", e)
