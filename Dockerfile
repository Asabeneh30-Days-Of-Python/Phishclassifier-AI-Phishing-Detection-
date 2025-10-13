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
# Use wheelhouse only (--find-links=/wheels) so builds cannot fall back to PyPI.
RUN if [ "${INSTALL_DEV}" = "1" ]; then \
      echo "Dev image: install runtime then dev extras from wheelhouse"; \
      pip install --no-cache-dir --find-links=/wheels --extra-index-url https://download.pytorch.org/whl/cpu -r /app/requirements-runtime.txt -c /app/constraints.txt && \
      if [ -f /app/requirements-dev.txt ] && [ -s /app/requirements-dev.txt ]; then \
        pip install --no-cache-dir --find-links=/wheels --extra-index-url https://download.pytorch.org/whl/cpu -r /app/requirements-dev.txt -c /app/constraints.txt; \
      fi; \
    else \
      echo "Prod image: install runtime only from wheelhouse"; \
      pip install --no-cache-dir --find-links=/wheels --extra-index-url https://download.pytorch.org/whl/cpu -r /app/requirements-runtime.txt -c /app/constraints.txt; \
    fi

# Re-enforce pinned packages from wheelhouse (force exact eventlet version required by gunicorn)
# Use --no-index --find-links so pip cannot fetch a different eventlet from PyPI
RUN pip install --no-cache-dir --find-links=/wheels --extra-index-url https://download.pytorch.org/whl/cpu --no-deps --force-reinstall eventlet==0.40.3

# Application source
COPY . .

# Ensure start-worker-io.sh is included in the image and executable
# This bakes the corrected, Unix-shebang script into the image so worker_io can exec it.
# Replace or remove this COPY if you prefer to run the inline command in docker-compose instead.
COPY start-worker-io.sh /app/start-worker-io.sh
RUN chmod +x /app/start-worker-io.sh || true

# install en_core_web_sm wheel from /wheels if present, then install NLTK vader_lexicon if nltk is installed
RUN if ls /wheels/en_core_web_sm-* 1> /dev/null 2>&1; then \
  echo "Installing en_core_web_sm from first matching wheel in /wheels"; \
  for f in /wheels/en_core_web_sm-*; do \
    pip install --no-index "$f" && break; \
  done; \
else \
  echo "No en_core_web_sm wheel found in /wheels; skipping model install (will fetch at runtime if needed)"; \
fi && \
if pip show nltk > /dev/null 2>&1; then \
  echo "Installing NLTK vader_lexicon into /root/nltk_data"; \
  python -c "import nltk, os; d=os.environ.get('NLTK_DATA','/root/nltk_data'); os.makedirs(d, exist_ok=True); import nltk.downloader as nd; nd.download('vader_lexicon', download_dir=d)"; \
else \
  echo "NLTK not installed or not in wheelhouse; skipping NLTK data download"; \
fi

EXPOSE 5000

ENV FLASK_APP= \
    FLASK_RUN_HOST= \
    FLASK_RUN_PORT=

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "wsgi.py"]
