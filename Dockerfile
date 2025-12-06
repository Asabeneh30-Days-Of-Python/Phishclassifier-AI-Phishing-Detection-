FROM phishclassifier-base:base as runtime

# Small runtime system deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini \
      dos2unix \
      bash \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PROJECT_ROOT=/app
ENV MODELS_DIR=/app/models
ENV SCRIPTS_DIR=/app/scripts
ENV MODEL_PATH=/app/models/phishing_model.pkl
ENV THRESHOLDS_PATH=/app/scripts/thresholds.json
ENV PYTHONPATH=/app
ENV NLTK_DATA=/app/nltk_data

RUN mkdir -p ${NLTK_DATA} ${SCRIPTS_DIR} ${MODELS_DIR} && chown -R root:root ${NLTK_DATA} ${SCRIPTS_DIR} ${MODELS_DIR}

# Copy application source (keep .dockerignore excluding wheels/datasets)
COPY . .

# Bake start-worker-wait.sh into the image, ensure LF and executable permissions
# (minimal, explicit change to avoid host bind-mount BOM/CRLF issues)
COPY start-worker-wait.sh /app/start-worker-wait.sh
RUN dos2unix /app/start-worker-wait.sh || true && chmod +x /app/start-worker-wait.sh

# Install the application into site-packages from the wheelhouse in the base image
# (phishclassifier-base:base already contains /wheels and runtime deps)
RUN python -m pip install --no-index --find-links=/wheels -r /app/requirements-runtime.txt -c /app/constraints.txt

# remove /app from PYTHONPATH in runtime images to avoid accidental shadowing
ENV PYTHONPATH=

# If you maintain pre-bundled nltk_data in the repo, copy it (optional)
COPY ./nltk_data ${NLTK_DATA}

# Copy Python in-process start wrapper for IO worker and ensure executable
COPY start-worker-io.py /app/start-worker-io.py
RUN chmod +x /app/start-worker-io.py

# Install NLTK at build time
RUN python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install --no-cache-dir "nltk==3.9.1"

# Run a separate heredoc Python step to download/check punkt (proper heredoc formatting)
RUN python - <<'PY'
import nltk, os, sys
d = os.environ.get("NLTK_DATA", "/app/nltk_data")
if d not in nltk.data.path:
    # make the mounted path highest priority
    nltk.data.path.insert(0, d)
# Download punkt if missing
try:
    nltk.data.find("tokenizers/punkt")
    print("punkt OK")
except LookupError:
    print("Downloading punkt...")
    nltk.download("punkt", quiet=True)
# Check vader_lexicon (optional)
try:
    nltk.data.find("sentiment/vader_lexicon")
    print("vader_lexicon OK")
except LookupError:
    print("vader_lexicon missing (not critical unless used)")
PY

EXPOSE 5000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "wsgi.py"]
