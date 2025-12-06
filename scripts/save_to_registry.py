#!/usr/bin/env python3
"""
scripts/save_to_registry.py

Uploads model artifact and thresholds to MLflow if available.
Safely handles missing mlflow and avoids "possibly unbound" Pylance warnings
by ensuring `mlflow` is defined at module scope and guarding runtime usage.
"""

import os
import json
import argparse
from typing import Optional, Any

# Ensure mlflow name always exists to satisfy static checkers
mlflow: Any = None
MLFLOW_AVAILABLE = False

# Safe import for mlflow; set flag and keep mlflow bound
try:
    import mlflow  # type: ignore[reportMissingImports]
    MLFLOW_AVAILABLE = True
except Exception:
    mlflow = None
    MLFLOW_AVAILABLE = False

def register_with_mlflow(model_path: str, thresholds_path: str, git_sha: str) -> None:
    """
    Log model and thresholds to MLflow and attempt to register the model.
    If mlflow is not available, this is a no-op with informative prints.
    """
    if not MLFLOW_AVAILABLE or mlflow is None:
        print("MLflow not installed or unavailable; skipping registry upload")
        return

    # configure tracking URI (can be overridden via env)
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))

    # Start a run and log artifacts; verify active run safely
    try:
        with mlflow.start_run() as _run:
            # Log model artifact if present
            if model_path and os.path.exists(model_path):
                try:
                    mlflow.log_artifact(model_path, artifact_path="model")
                except Exception as e:
                    print(f"Failed to log model artifact {model_path}: {e}")
            else:
                print(f"Model path missing or not found: {model_path}")

            # Log thresholds artifact if present
            if thresholds_path and os.path.exists(thresholds_path):
                try:
                    mlflow.log_artifact(thresholds_path, artifact_path="thresholds")
                except Exception as e:
                    print(f"Failed to log thresholds artifact {thresholds_path}: {e}")
            else:
                print(f"Thresholds path missing or not found: {thresholds_path}")

            # Tag run with git sha for traceability
            if git_sha:
                try:
                    mlflow.set_tag("git_sha", git_sha)
                except Exception as e:
                    print(f"Warning: failed to set git_sha tag: {e}")

            # Safely obtain run_id for registration attempt
            active = mlflow.active_run()
            run_id: Optional[str] = None
            if active is not None:
                info = getattr(active, "info", None)
                if info is not None:
                    run_id = getattr(info, "run_id", None)

            if run_id:
                model_artifact_path = os.path.join("model", os.path.basename(model_path))
                model_uri = f"runs:/{run_id}/{model_artifact_path}"
                try:
                    mlflow.register_model(model_uri, "PhishClassifier")
                    print(f"Model registration attempted for {model_uri}")
                except Exception as e:
                    print(f"Model registration skipped or failed: {e}")
            else:
                print("No active run ID available; skipping register_model call")
    except Exception as e:
        print(f"MLflow run failed: {e}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Save model and thresholds to MLflow registry")
    parser.add_argument("--model", required=True, help="Path to model file")
    parser.add_argument("--thresholds", required=True, help="Path to thresholds.json")
    parser.add_argument("--git-sha", required=False, default="", help="Git SHA for traceability")
    args = parser.parse_args()

    register_with_mlflow(args.model, args.thresholds, args.git_sha)

if __name__ == "__main__":
    main()