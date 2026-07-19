"""Lazy Postgres connection helper (psycopg2)."""

import contextlib
import hashlib

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


@contextlib.contextmanager
def advisory_lock(name: str):
    """Cluster-wide mutex for a critical section, keyed by an arbitrary string (PR #7 review).

    Concurrent callers with the same name -- including on different workers/processes --
    serialize here; the lock is released when the block exits (commit) or the connection
    drops (crash), so it can never wedge permanently.

    Uses a DEDICATED short-lived connection, NOT the module's shared autocommit connection:
    that connection is a single libpq handle shared across the sync threadpool (not
    thread-safe), and a session advisory lock on it would be one shared owner, serializing
    nothing. A private connection per caller makes each a distinct PG session, so
    pg_advisory_xact_lock is a real mutex; running it in a transaction (autocommit off, the
    psycopg2 default) means the lock auto-releases on commit/rollback.
    """
    # Fold the name to a signed 64-bit int (pg_advisory_xact_lock takes a bigint).
    key = int.from_bytes(
        hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest(),
        "big",
        signed=True,
    )
    conn = psycopg2.connect(DATABASE_URL)  # own session; transaction (autocommit off)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        yield
        conn.commit()  # releases the transaction-scoped advisory lock
    except Exception:
        conn.rollback()  # also releases the lock
        raise
    finally:
        conn.close()
