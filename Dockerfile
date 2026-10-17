# syntax=docker/dockerfile:1
# Multi-stage: wheels are built once in a throwaway layer so the runtime image
# carries no compilers and no build cache.
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt


FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    CHAINMIND_DB=/data/chainmind.db

# Non-root: the container never needs to write outside /data.
RUN useradd --create-home --uid 10001 chainmind \
 && mkdir -p /data && chown -R chainmind:chainmind /data

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=chainmind:chainmind chainmind ./chainmind
COPY --chown=chainmind:chainmind scripts ./scripts

USER chainmind
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=4).status==200 else 1)"

# Railway/Cloud Run inject $PORT. One worker on purpose: the SQLite writer and
# the event bus are per-process, and the workload is IO-bound on Google APIs,
# so asyncio concurrency inside one process beats multiple processes here.
CMD ["sh", "-c", "exec uvicorn chainmind.api:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 65 --no-access-log"]
