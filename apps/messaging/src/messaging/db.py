"""Explicit versioned database initialization; API startup never invents migrations."""
from functools import lru_cache
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine
from .config import settings
from .models import SchemaRevision, ProviderAccount

SCHEMA_VERSION = 1


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
        if row and row.version != SCHEMA_VERSION:
            raise RuntimeError("Unsupported messaging schema version; use a reviewed migration")
        if not row:
            SQLModel.metadata.create_all(conn)
            conn.execute(text("INSERT INTO schema_revision (id, version) VALUES (1, :version)"), {"version": SCHEMA_VERSION})
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
