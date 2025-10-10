# Dockerfile — runtime image installs only from an external wheelhouse (COPY ./wheels -> /wheels)
FROM python:3.11-slim-bullseye AS runtime

# Avoid apt hash fetch problems
RUN rm -rf /var/lib/apt/lists/* \
 && printf 'Acquire::By-Hash "false";\n' > /etc/apt/apt.conf.d/99nohash

# Install small runtime system deps (no wheel-building here)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini \
      libatlas3-base \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=120

# Expect a prebuilt wheelhouse to be copied into the build context at ./wheels
# CI: place wheelhouse in repo build context before docker build (see workflows).
COPY ./wheels /wheels
COPY constraints.txt /app/constraints.txt

# Copy requirements files (runtime and optional dev)
COPY requirements-runtime.txt requirements-dev.txt /app/

# Build-time switch: INSTALL_DEV=1 installs dev/training deps into image, default is 0 (small runtime)
ARG INSTALL_DEV=0

# Install runtime by default; only install dev extras when explicitly requested.
# Use wheelhouse only (--no-index --find-links=/wheels) so builds cannot fall back to PyPI.
RUN if [ "${INSTALL_DEV}" = "1" ]; then \
      echo "Dev image: install runtime then dev extras from wheelhouse"; \
      pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements-runtime.txt -c /app/constraints.txt && \
      if [ -f /app/requirements-dev.txt ] && [ -s /app/requirements-dev.txt ]; then \
        pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements-dev.txt -c /app/constraints.txt; \
      fi; \
    else \
      echo "Prod image: install runtime only from wheelhouse"; \
      pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements-runtime.txt -c /app/constraints.txt; \
    fi

# Re-enforce pinned packages from wheelhouse (force exact eventlet version required by gunicorn)
# Use --no-index --find-links so pip cannot fetch a different eventlet from PyPI
RUN pip install --no-cache-dir --no-index --find-links=/wheels --no-deps --force-reinstall eventlet==0.33.0 \
 && pip install --no-cache-dir --no-index --find-links=/wheels --force-reinstall numpy==1.23.5 || true

# Application source
COPY . .

# Optional: install spaCy model and NLTK lexicon from wheelhouse (deterministic)
# - place en_core_web_sm-3.8.0-py3-none-any.whl into ./wheels before building
# - if the model wheel is absent, this step will be skipped (avoids network access)
RUN if ls /wheels/en_core_web_sm-* 1> /dev/null 2>&1; then \
      echo "Installing spaCy model from wheelhouse"; \
      pip install --no-index --find-links=/wheels /wheels/en_core_web_sm-3.8.0-py3-none-any.whl || pip install --no-index --find-links=/wheels en_core_web_sm; \
    else \
      echo "No en_core_web_sm wheel found in /wheels; skipping model install (will fetch at runtime if needed)"; \
    fi && \
    if pip show nltk > /dev/null 2>&1; then \
      echo "Installing NLTK vader_lexicon data into /root/nltk_data (only if reachable)"; \
      python -c "import nltk, sys; import os; d=os.environ.get('NLTK_DATA','/root/nltk_data'); os.makedirs(d, exist_ok=True); nltk.data.path.append(d); import nltk.downloader as nd; nd.download('vader_lexicon', download_dir=d)"; \
    else \
      echo "NLTK not installed or not in wheelhouse; skipping NLTK data download"; \
    fi

EXPOSE 5000

ENV FLASK_APP= \
    FLASK_RUN_HOST= \
    FLASK_RUN_PORT=

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "wsgi.py"]
