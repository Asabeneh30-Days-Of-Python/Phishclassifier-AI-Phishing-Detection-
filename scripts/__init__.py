# scripts/__init__.py
from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Any

ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[1]))

def project_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)

def load_json(name: str) -> Any:
    p = project_path("scripts", name)
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)
