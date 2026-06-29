"""SQLAlchemy engine/session (lazy).

Only read paths would use SQLAlchemy. The money-moving charge path
(payments.charge) still uses raw psycopg2 (db.py) — the float-money + no-idempotency
debt lives there, unmigrated.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import DATABASE_URL

_engine = None
_Session = None


def _init():
    global _engine, _Session
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        _Session = sessionmaker(bind=_engine, autoflush=False, future=True)


def get_session():
    _init()
    session = _Session()
    try:
        yield session
    finally:
        session.close()
