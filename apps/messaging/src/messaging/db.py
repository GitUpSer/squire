"""Explicit versioned database initialization; API startup never invents migrations."""
from functools import lru_cache
import time
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine
from .config import settings
from .models import SchemaRevision, ProviderAccount

SCHEMA_VERSION = 2


@lru_cache
def engine():
    cfg = settings()
    cfg.check_secrets()
    url = cfg.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True,
                         connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})


def migrate() -> None:
    eng = engine()
    with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(784231901)"))
        SchemaRevision.__table__.create(conn, checkfirst=True)
        row = conn.execute(text("SELECT version FROM schema_revision WHERE id=1")).first()
        if row and row.version not in {1, SCHEMA_VERSION}:
            raise RuntimeError("Unsupported messaging schema version; use a reviewed migration")
        if not row:
            SQLModel.metadata.create_all(conn)
            conn.execute(text("INSERT INTO schema_revision (id, version) VALUES (1, :version)"), {"version": SCHEMA_VERSION})
        elif row.version == 1:
            conn.execute(text("ALTER TABLE inbox_event ADD COLUMN expires_at BIGINT"))
            conn.execute(text("ALTER TABLE outbox_message ADD COLUMN cause_event_id VARCHAR"))
            conn.execute(text("CREATE INDEX ix_outbox_message_cause_event_id ON outbox_message (cause_event_id)"))
            # Old events did not record whether expiry was known; never infer ordinary.
            conn.execute(text("UPDATE inbox_event SET expires_at=0 WHERE kind='message'"))
            # Legacy replies did not persist parent IDs. Preserve ambiguous send
            # outcomes, but never resume their content without expiry provenance.
            conn.execute(text("""UPDATE outbox_message SET expires_at=0, payload_enc=NULL,
                error_code='legacy_expiry_unknown', completed_at=COALESCE(completed_at, :now),
                state=CASE WHEN state IN ('dispatching','uncertain') THEN 'uncertain' ELSE 'cancelled' END
                WHERE runtime_id IS NOT NULL AND challenge_id IS NULL
                AND state IN ('queued','claimed','dispatching','uncertain')"""), {"now": time.time()})
            conn.execute(text("UPDATE schema_revision SET version=2 WHERE id=1"))
    with Session(eng) as db:
        account = db.get(ProviderAccount, settings().provider_account_id)
        if account is None:
            db.add(ProviderAccount(id=settings().provider_account_id))
            db.commit()


def verify_schema() -> None:
    with Session(engine()) as db:
        row = db.get(SchemaRevision, 1)
        if row is None or row.version != SCHEMA_VERSION:
            raise RuntimeError("Run squire-messaging-migrate before starting the service")


def main() -> None:
    migrate()
    print(f"Messaging schema ready at revision {SCHEMA_VERSION}; outbound remains gated")
