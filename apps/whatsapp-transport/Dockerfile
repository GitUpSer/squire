FROM golang:1.27.3-bookworm AS wacli
WORKDIR /build
RUN git init . && git remote add origin https://github.com/openclaw/wacli.git \
    && git fetch --depth 1 origin 535a8b969cc83c3aad57f66be77e8fd4a7fcaaa3 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 535a8b969cc83c3aad57f66be77e8fd4a7fcaaa3
COPY apps/whatsapp-transport/patches/0001-durable-message-origin.patch /tmp/origin.patch
COPY apps/whatsapp-transport/patches/squire_origin_test.go internal/store/squire_origin_test.go
COPY apps/whatsapp-transport/patches/squire_expiry_store_test.go internal/store/squire_expiry_store_test.go
COPY apps/whatsapp-transport/patches/squire_expiry_app_test.go internal/app/squire_expiry_app_test.go
COPY apps/whatsapp-transport/patches/squire_expiry_wa_test.go internal/wa/squire_expiry_wa_test.go
COPY apps/whatsapp-transport/patches/squire_expiry_cli_test.go cmd/wacli/squire_expiry_cli_test.go
RUN echo 'e51a043ca961f3cf872309dc520546194fa562d5663fe134de6059c4f3a22db2  /tmp/origin.patch' | sha256sum -c - \
    && git apply --check /tmp/origin.patch && git apply /tmp/origin.patch \
    && go test -tags sqlite_fts5 ./internal/store ./internal/wa ./internal/app ./cmd/wacli -run 'TestSquire|TestArchiveIncomingConcurrentWriters|TestUnpacedRefusesExpiredCallerDeadlineWithFreeSlot' \
    && CGO_ENABLED=1 go build -tags sqlite_fts5 -trimpath -ldflags '-s -w -X main.version=0.26.0-squire.2' -o /usr/local/bin/wacli ./cmd/wacli

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080 TRANSPORT_DATA_DIR=/data PYTHONPATH=/opt/backup
WORKDIR /app
COPY --from=wacli /usr/local/bin/wacli /usr/local/bin/wacli
COPY apps/whatsapp-transport/pyproject.toml apps/whatsapp-transport/requirements.lock ./
COPY apps/whatsapp-transport/src ./src
COPY apps/whatsapp-transport/wacli-release.json ./
COPY apps/whatsapp-transport/scripts/entrypoint.py /usr/local/bin/transport-entrypoint.py
COPY infra/install_age.py /tmp/install_age.py
COPY infra/migrate_personal_agent.py infra/pilot_inventory.py /opt/backup/
RUN python /tmp/install_age.py && pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home transport && mkdir /data && chown transport:transport /data \
    && wacli --version
USER root
EXPOSE 8080
ENTRYPOINT ["python", "/usr/local/bin/transport-entrypoint.py"]
