# control-api image. Railway builds this on merge (implementation-plan Task 0.1).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Frozen runtime dependency graph exported from the pre-existing uv.lock.
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Real source last -- this is the layer that actually changes between deploys.
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Run unprivileged. Nothing here writes to the filesystem at runtime (state lives
# in Postgres), so an unwritable /app is a feature.
RUN useradd --create-home --uid 10001 squire
USER squire

# Railway injects $PORT; the default matches the tenant/ingress convention.
ENV PORT=8080
EXPOSE 8080

# Console entry point prebinds a real dual-stack socket for Railway probes and
# private networking; uvicorn --host :: alone creates an IPv6-only listener.
CMD ["squire-control-api"]
