#!/usr/bin/env python3
"""Read only queue metadata; never emit retained payloads."""
import json

import psycopg2
from psycopg2 import sql
from pilot_restore_drill import DATABASES, connection


def inspect():
    env = connection(DATABASES["hindsight-source"])
    with psycopg2.connect(host=env["PGHOST"], port=env["PGPORT"], user=env["PGUSER"],
                         password=env["PGPASSWORD"], dbname=env["PGDATABASE"], options=env["PGOPTIONS"]) as db:
        db.set_session(readonly=True)
        with db.cursor() as cursor:
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='async_operations' ORDER BY ordinal_position")
            columns = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT status,count(*) FROM public.async_operations GROUP BY status ORDER BY status")
            result = {"status_counts": dict(cursor.fetchall()), "columns": columns}
            kind = next((name for name in ("operation_type", "task_type", "type") if name in columns), None)
            if kind:
                cursor.execute(sql.SQL("SELECT {},status,count(*) FROM public.async_operations GROUP BY {},status ORDER BY {},status").format(*[sql.Identifier(kind)] * 3))
                result["type_status_counts"] = cursor.fetchall()
    return result


if __name__ == "__main__":
    print(json.dumps(inspect(), sort_keys=True))
