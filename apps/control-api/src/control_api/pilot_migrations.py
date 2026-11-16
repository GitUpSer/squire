"""Versioned, additive pilot migration. Run before enabling public routes."""
from sqlalchemy import text, inspect
from sqlmodel import SQLModel, Session
from control_api import pilot_models, models  # registers metadata
from control_api.pilot_models import PilotCapacity, PilotSchemaRevision


def migrate(engine):
    # Transaction-scoped advisory lock serializes production deploy migrations.
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(741920260)"))
        names = {"tenant", "bot", "heartbeat", "waitlist", "provisionjob", "pilotaccount", "pilotauthchallenge", "pilotsession", "pilotinvitation", "pilotcapacity", "pilotadmission", "pilotruntime", "pilotentitlement", "pilotturn", "pilotoperation", "pilotratebucket", "pilotschemarevision", "pilotextension"}
        SQLModel.metadata.create_all(conn, tables=[SQLModel.metadata.tables[name] for name in names])
        # create_all does not ALTER. Refuse startup on incompatible existing pilot
        # tables rather than silently mark an incomplete schema as migrated.
        inspector = inspect(conn)
        for name in names:
            if name.startswith("pilot"):
                actual = {column["name"] for column in inspector.get_columns(name)}
                expected = set(SQLModel.metadata.tables[name].columns.keys())
                if actual != expected: raise RuntimeError(f"pilot schema migration required for {name}")
        with Session(bind=conn) as session:
            if session.exec(__import__("sqlmodel").select(PilotSchemaRevision).where(PilotSchemaRevision.id > 2)).first():
                raise RuntimeError("database pilot revision is newer than this application")
            if session.get(PilotSchemaRevision, 1) is None:
                session.add(PilotSchemaRevision(id=1))
            if session.get(PilotSchemaRevision, 2) is None:
                session.add(PilotSchemaRevision(id=2))
            if session.get(PilotCapacity, "pilot") is None:
                session.add(PilotCapacity(id="pilot"))
            session.flush()


def main():
    from control_api.db import get_engine
    migrate(get_engine())


if __name__ == "__main__":
    main()
