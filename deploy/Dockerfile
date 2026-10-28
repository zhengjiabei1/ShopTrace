# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBAGENT_CHROMIUM_PATH=/usr/bin/chromium \
    WEBAGENT_DATA_DIR=/data \
    SHOPTRACE_REPORT_DIR=/app/reports
WORKDIR /app

# Install the browser once; mount a session CA only when pip needs it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates chromium fonts-noto-cjk tini \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock.txt requirements-webagent.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r requirements-webagent.txt
COPY lab ./lab
COPY webagent ./webagent
COPY experiments ./experiments
COPY THIRD_PARTY_NOTICES.md ./
COPY portal/dist ./reports
RUN chmod -R a+rX /app/lab /app/webagent /app/reports \
    && useradd --uid 10001 --create-home shoptrace \
    && mkdir -p /data && chown shoptrace:shoptrace /data
USER shoptrace
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; port=os.getenv('PORT',os.getenv('WEBAGENT_HOSTED_PORT','8080')); urllib.request.urlopen('http://127.0.0.1:'+port+'/healthz', timeout=3)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "webagent.hosted"]
