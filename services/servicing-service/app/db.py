"""Lazy Postgres connection helper (psycopg2)."""

import psycopg2
import psycopg2.extras
from .config import DATABASE_URL

_conn = None

# The client's answer of 2026-08-14 makes the reconciliation cut-off the processor-settled
# date in UTC, so the day boundary is part of a money control rather than a display detail.
# `payments.created_at` is TIMESTAMPTZ (db/init/001_schema.sql:138) and reconciliation
# compares `created_at.date()` against the settlement date; an unpinned session resolves
# that date in whatever timezone the server happens to be set to, and nothing in
# docker-compose.yml pins TZ/PGTZ. A payment at 23:30 UTC then reads as the next day on a
# UTC+1 database and lands on the wrong side of the window. Pinned here, at the one place
# every read in this service goes through, rather than per-query.
_SESSION_OPTIONS = "-c timezone=UTC"


def get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL, options=_SESSION_OPTIONS)
        _conn.autocommit = True
    return _conn


def query(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if cur.description:
            return cur.fetchall()
        return []
