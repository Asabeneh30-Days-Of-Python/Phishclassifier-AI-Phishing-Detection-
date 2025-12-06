
#!/usr/bin/env python3
# scripts/train_and_publish.py
import os, json, argparse, datetime, subprocess
from train_model import train_and_save_model  # repository train function

def write_metadata(out_dir, git_sha, metrics, thresholds):
    payload = {
      "model": "phishing_model.pkl",
      "created_at": datetime.datetime.utcnow().isoformat() + "Z",
      "thresholds": thresholds,
      "validation": metrics,
      "git_sha": git_sha
    }
    with open(os.path.join(out_dir, "metadata.json"), "w") as fh:
        json.dump(payload, fh, indent=2)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--thresholds", required=True)
    p.add_argument("--git-sha", default=os.getenv("GITHUB_SHA", "local"))
    args = p.parse_args()

    out_model = train_and_save_model(datasets=["sample"], output_path=args.output)
    # simple validation hook: load model and run a health check (implement in train_model)
    # Here, compute some dummy metrics and thresholds for example
    metrics = {"auroc": 0.99}
    thresholds = {"quarantine": 0.75, "review": 0.5, "accept": 0.25}
    with open(args.thresholds, "w") as fh:
        json.dump({
            "model": os.path.basename(out_model),
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
            "thresholds": thresholds,
            "validation": metrics,
            "git_sha": args.git_sha
        }, fh, indent=2)
    write_metadata(os.path.dirname(args.output), args.git_sha, metrics, thresholds)
    print("Model and thresholds written:", out_model, args.thresholds)

if __name__ == "__main__":
    main()
