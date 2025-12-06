# tools/pseudo_label_and_save.py
import os
from api.classifier import _load_model, MODEL_PATH

SRC = os.path.join("data", "unlabeled.txt")
OUT_TEXTS = os.path.join("data", "texts.txt")
OUT_LABELS = os.path.join("data", "labels.txt")

pipe = _load_model(MODEL_PATH)

texts, labels = [], []
if os.path.exists(SRC):
    with open(SRC, "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if not t: 
                continue
            pred = pipe.predict([t])[0]
            lab = 1 if (pred == "Phishing" or str(pred) in ("1", "True", "true")) else 0
            texts.append(t)
            labels.append(lab)

# Optionally: keep all positives and sample negatives to balance
positives = [(t,l) for t,l in zip(texts,labels) if l==1]
negatives = [(t,l) for t,l in zip(texts,labels) if l==0]
# keep all positives, sample negatives up to len(positives)*3 or 1000
import random
random.seed(42)
neg_sample = random.sample(negatives, min(max(10, len(positives)*3), len(negatives))) if negatives else []
final = positives + neg_sample
if not final:
    # fallback: use original small seeds
    final = [
        ("your account under attack", 1),
        ("reset your password now", 1),
        ("monthly newsletter", 0),
        ("meeting agenda", 0),
    ]

with open(OUT_TEXTS, "w", encoding="utf-8") as ft, open(OUT_LABELS, "w", encoding="utf-8") as fl:
    for t,l in final:
        ft.write(t + "\n")
        fl.write(str(int(l)) + "\n")

print("Saved pseudo-labeled dataset:", len(final), "examples ->", OUT_TEXTS, OUT_LABELS)
