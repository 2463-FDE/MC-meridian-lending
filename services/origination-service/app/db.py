"""Lazy Postgres connection helper (psycopg2)."""

from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from .config import DATABASE_URL

_conn = None


def get_conn():
    """Open the connection on first use so importing the app needs no DB."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL)
        _conn.autocommit = True
    return _conn


def query(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if cur.description:
            return cur.fetchall()
        return []


@contextmanager
def transaction():
    """A short-lived NON-autocommit connection for a multi-statement atomic write.

    query() runs each statement on a shared autocommit connection -- correct for the
    single-statement money paths, but wrong for a multi-row delete that must be all-or-nothing.
    The compensating abandon (routers/applications.py) deletes an application, its KYC rows,
    and its applicant together; under per-statement autocommit a mid-sequence failure would
    leave a partially-deleted applicant with orphaned PII. This commits on clean exit and rolls
    back on any exception. Dedicated connection (not the shared autocommit one) so the
    transaction is isolated. Yields a RealDictCursor."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
