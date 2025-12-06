# Base image that installs the full wheelhouse once
FROM python:3.11-slim-bullseye AS base

# Reduce apt list churn
RUN rm -rf /var/lib/apt/lists/* \
 && printf 'Acquire::By-Hash "false";\n' > /etc/apt/apt.conf.d/99nohash

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal system deps needed only for runtime; keep build deps out unless required
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Copy deterministic inputs
COPY requirements-runtime.txt /app/requirements-runtime.txt
COPY constraints.txt /app/constraints.txt

# Copy wheelhouse into base image for one-time installs (expect CI to supply a verified wheelhouse)
COPY wheels /wheels
COPY wheels/.disabled /wheels_disabled

# Use bash for following RUN heredocs so the container executes them with bash reliably
SHELL ["/bin/bash","-eux","-o","pipefail","-c"]

# Populate missing wheels from the disabled archive only when absent and only allowed wheel tags
RUN <<'BASH'
# show wheelhouse and disabled archive
echo "Wheelhouse:"; ls -1 /wheels 2>/dev/null || true
echo "Disabled archive:"; ls -1 /wheels_disabled 2>/dev/null || true

mkdir -p /wheels /wheels_disabled
filter='py3-none-any\.whl|manylinux|musllinux|linux'

for f in /wheels_disabled/*; do
  [ -e "$f" ] || continue
  base=$(basename "$f")
  [ -n "$base" ] || continue
  if [ -f "/wheels/$base" ]; then
    echo "Skipping existing wheel: $base"
    continue
  fi
  # strip any stray CR and match case-insensitively
  if printf '%s\n' "$base" | tr -d '\r' | grep -Ei -q "$filter"; then
    echo "Restoring $base from disabled archive"
    cp -p -- "$f" "/wheels/$base"
  else
    echo "Filtered out incompatible wheel: $base"
  fi
done

echo "Final wheelhouse count after restore: $(ls -1 /wheels 2>/dev/null | wc -l)"
ls -1 /wheels || true
BASH

# Attempt to download any still-missing wheels referenced by requirements-runtime.txt into /wheels
# Python-based parser to avoid shell/awk quoting pitfalls; downloads wheels only (--only-binary)
RUN <<'PY'
#!/usr/bin/env python3
import sys, subprocess, pathlib, re

req_file = pathlib.Path("/app/requirements-runtime.txt")
dest = pathlib.Path("/wheels")
dest.mkdir(parents=True, exist_ok=True)

def iter_reqs(p: pathlib.Path):
    for line in p.read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        yield s

def pkg_name(req: str):
    m = re.match(r"^\s*([A-Za-z0-9_.+-]+)", req)
    return m.group(1).lower() if m else req.lower()

existing = [f.name.lower() for f in dest.iterdir() if f.is_file()]
for req in iter_reqs(req_file):
    name = pkg_name(req)
    found = any(re.match(rf"^{re.escape(name)}[-_].*", n) or n.startswith(name) for n in existing)
    if found:
        print(f"Found existing wheel(s) for {name}, skipping download")
        continue
    print(f"No wheel found for {name} locally — attempting to download a wheel from PyPI into /wheels")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    rc = subprocess.call([sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "--dest", str(dest), req])
    if rc != 0:
        print("DOWNLOAD_FAILED:", req)
    existing = [f.name.lower() for f in dest.iterdir() if f.is_file()]

print("Final wheelhouse count after downloads:", len(existing))
PY

# Revert SHELL to default sh -c for subsequent RUNs that use explicit bash invocation
SHELL ["/bin/sh","-eux","-c"]

# Install runtime requirements: prefer wheelhouse but allow fetch-from-PyPI fallback for packages not found in /wheels
RUN bash -lc "set -euo pipefail && \
    python -m pip install --upgrade pip && \
    # ensure setuptools and wheel are available from wheelhouse if present
    python -m pip install --no-index --find-links=/wheels --no-cache-dir setuptools wheel || true && \
    # try strict offline install first; if it fails (missing wheels/build-deps), retry allowing PyPI for missing packages
    python -m pip install --no-index --find-links=/wheels -r /app/requirements-runtime.txt -c /app/constraints.txt --no-cache-dir || \
    python -m pip install --find-links=/wheels -r /app/requirements-runtime.txt -c /app/constraints.txt --no-cache-dir && \
    python -m pip check"

# Re-enforce eventlet pinned version from wheelhouse (if required)
RUN python -m pip install --no-index --find-links=/wheels --no-deps --force-reinstall eventlet==0.40.3 || true

# Optionally install any model wheels present (en_core_web_sm) into site-packages
RUN if ls /wheels/en_core_web_sm-* 1> /dev/null 2>&1; then \
      for f in /wheels/en_core_web_sm-*; do \
        python -m pip install --no-index "$f" && break; \
      done; \
    fi

# Finalize base image
ENV MODELS_DIR=/app/models
ENV SCRIPTS_DIR=/app/scripts
