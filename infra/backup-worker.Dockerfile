FROM postgres:18-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/backup
COPY infra/install_age.py /tmp/install_age.py
RUN python3 /tmp/install_age.py && python3 -m venv /opt/backup/venv \
    && /opt/backup/venv/bin/pip install --no-cache-dir boto3==1.42.34 httpx==0.28.1
COPY infra/pilot_backup_worker.py infra/migrate_personal_agent.py infra/pilot_inventory.py ./
ENV PATH=/opt/backup/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONUNBUFFERED=1
RUN useradd --uid 10002 --create-home backup-worker
USER 10002:10002
ENTRYPOINT ["python", "/opt/backup/pilot_backup_worker.py"]
CMD ["daily"]
