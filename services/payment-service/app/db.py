"""Lazy Postgres connection helper (psycopg2)."""
import psycopg2
import psycopg2.extras
from .config import DATABASE_URL

_conn = None


def get_conn():
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
