import joblib, pprint, os
p = r"C:\Users\Charles Keter\Documents\PhishClassifier\models\surrogate_explainer.joblib"
if not os.path.exists(p):
    print("surrogate file not found:", p)
    raise SystemExit(1)
obj = joblib.load(p)
pprint.pprint(obj)
print("coef len:", None if obj.get("coef") is None else len(obj["coef"]))
print("vocab_size:", obj.get("vocab_size"))
