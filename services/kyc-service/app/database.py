"""SQLAlchemy engine/session (lazy).

Only the read paths use SQLAlchemy. The kyc_checks write path still uses raw psycopg2
(db.py) — carried over from origination as-is when this service was split out.
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
