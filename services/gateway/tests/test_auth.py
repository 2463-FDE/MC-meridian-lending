"""D27 regression: password hashing must be salted, and legacy unsalted rows must
still authenticate and migrate themselves off the old scheme on first successful login.
"""

import hashlib

from app import auth


def _legacy_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def test_hash_password_is_salted():
    # The pre-fix defect: identical passwords produced identical hashes with no salt,
    # so one precomputed table covered every user. Two calls for the same password
    # must not collide.
    a = auth.hash_password("hunter2")
    b = auth.hash_password("hunter2")
    assert a != b


def test_hash_password_is_not_bare_sha256():
    stored = auth.hash_password("hunter2")
    assert stored != _legacy_hash("hunter2")
    assert stored.startswith("pbkdf2_sha256$")


def test_authenticate_accepts_correct_password_on_new_format(monkeypatch):
    stored = auth.hash_password("hunter2")
    calls = []
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda sql, params=None: (
            calls.append((sql, params))
            or [
                {
                    "id": 1,
                    "username": "maria",
                    "role": "borrower",
                    "display_name": "Maria",
                    "password_hash": stored,
                    "is_active": True,
                }
            ]
            if "SELECT" in sql
            else []
        ),
    )
    user = auth.authenticate("maria", "hunter2")
    assert user == {"id": 1, "username": "maria", "role": "borrower", "name": "Maria"}
    # Already on the new format -- no rehash UPDATE should fire.
    assert not any("UPDATE" in sql for sql, _ in calls)


def test_authenticate_rejects_wrong_password_on_new_format(monkeypatch):
    stored = auth.hash_password("hunter2")
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda sql, params=None: (
            [
                {
                    "id": 1,
                    "username": "maria",
                    "role": "borrower",
                    "display_name": "Maria",
                    "password_hash": stored,
                    "is_active": True,
                }
            ]
            if "SELECT" in sql
            else []
        ),
    )
    assert auth.authenticate("maria", "wrong") is None


def test_authenticate_still_accepts_legacy_unsalted_hash(monkeypatch):
    legacy = _legacy_hash("password")
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda sql, params=None: (
            [
                {
                    "id": 5,
                    "username": "admin",
                    "role": "admin",
                    "display_name": "Admin",
                    "password_hash": legacy,
                    "is_active": True,
                }
            ]
            if "SELECT" in sql
            else []
        ),
    )
    user = auth.authenticate("admin", "password")
    assert user is not None
    assert user["username"] == "admin"


def test_authenticate_rejects_wrong_password_on_legacy_hash(monkeypatch):
    legacy = _legacy_hash("password")
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda sql, params=None: (
            [
                {
                    "id": 5,
                    "username": "admin",
                    "role": "admin",
                    "display_name": "Admin",
                    "password_hash": legacy,
                    "is_active": True,
                }
            ]
            if "SELECT" in sql
            else []
        ),
    )
    assert auth.authenticate("admin", "wrong") is None


def test_authenticate_rehashes_legacy_row_on_successful_login(monkeypatch):
    # The D27 fix must drain existing rows to the new format without a forced reset:
    # a successful legacy-hash login writes a new pbkdf2_sha256$... hash back to the row.
    legacy = _legacy_hash("password")
    calls = []

    def _fake_query(sql, params=None):
        calls.append((sql, params))
        if "SELECT" in sql:
            return [
                {
                    "id": 5,
                    "username": "admin",
                    "role": "admin",
                    "display_name": "Admin",
                    "password_hash": legacy,
                    "is_active": True,
                }
            ]
        return []

    monkeypatch.setattr(auth.db, "query", _fake_query)
    user = auth.authenticate("admin", "password")
    assert user is not None

    update_calls = [c for c in calls if "UPDATE" in c[0]]
    assert len(update_calls) == 1
    update_sql, update_params = update_calls[0]
    assert "password_hash" in update_sql
    new_hash, user_id = update_params
    assert new_hash.startswith("pbkdf2_sha256$")
    assert user_id == 5
    # The rehashed value must itself verify against the same password.
    assert auth._verify_password("password", new_hash)


def test_inactive_user_is_rejected_regardless_of_hash_format(monkeypatch):
    stored = auth.hash_password("hunter2")
    monkeypatch.setattr(
        auth.db,
        "query",
        lambda sql, params=None: (
            [
                {
                    "id": 1,
                    "username": "maria",
                    "role": "borrower",
                    "display_name": "Maria",
                    "password_hash": stored,
                    "is_active": False,
                }
            ]
            if "SELECT" in sql
            else []
        ),
    )
    assert auth.authenticate("maria", "hunter2") is None
