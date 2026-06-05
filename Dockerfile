FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock .
# Generous timeout/retries so a slow or flaky network does not fail the build:
# pip's default 15s socket timeout is easy to exceed on a large wheel.
# --require-hashes installs the pinned, hash-verified lockfile, so the build is
# reproducible and tamper-resistant. Regenerate the lock with:
#   uv pip compile requirements.txt --generate-hashes -o requirements.lock
RUN pip install --no-cache-dir --require-hashes --timeout=100 --retries=10 -r requirements.lock

COPY . .
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

EXPOSE 8000

# Liveness probe baked into the image so any orchestrator (not only Compose) can
# health-check the container.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2).read()" || exit 1

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
