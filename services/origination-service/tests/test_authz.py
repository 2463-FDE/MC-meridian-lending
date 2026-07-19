"""ADR 0010 officer-OR-owner authorization (PR review).

The /los proxy reaches origination anonymously, so the application-scoped routes must
authorize the caller themselves: an officer (underwriter/admin) may act on any
application, the owning borrower may act only on their own, and everyone else --
including an anonymous caller with no X-User-Id -- is denied. A non-owner is denied as
404, never 403-on-exists, so a caller cannot enumerate which application ids are real.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import authz, config
from app.database import get_session
from app.main import app
from app.routers import applications

_FUTURE = datetime.now(timezone.utc) + timedelta(days=1)
_PAST = datetime.now(timezone.utc) - timedelta(days=1)

# A healthy CONTINUATION_TOKEN_KEYS is set for the whole suite by tests/conftest.py; the
# rotation / fallback / refusal tests below monkeypatch it again to model other configs.


def _authz_db(
    user_row, app_applicant_id, app_token=None, app_exists=None, expires_at=_FUTURE
):
    """Stub authz.db.query: the users lookup returns user_row ([] = no such user); the
    applications lookup returns the app's applicant_id + stored token digest + token expiry
    ([] = no such application). app_token is the RAW token; the stub stores its keyed hash
    (mirroring intake), so a test that presents the same raw token authorizes. app_exists
    defaults to "the app row is present iff it has an applicant_id or a token"; pass it
    explicitly to model an owner-less, token-less row that still exists. expires_at defaults
    to a future instant; pass _PAST to model an expired token."""

    exists = app_exists
    if exists is None:
        exists = app_applicant_id is not None or app_token is not None
    stored = authz.hash_token(app_token) if app_token is not None else None

    def _q(sql, params=None):
        if "FROM users" in sql:
            return [user_row] if user_row is not None else []
        if "FROM applications" in sql:
            return (
                [
                    {
                        "applicant_id": app_applicant_id,
                        "continuation_token": stored,
                        "continuation_token_expires_at": expires_at,
                    }
                ]
                if exists
                else []
            )
        raise AssertionError(f"unexpected query: {sql}")

    return _q


# --- require_officer_or_owner -------------------------------------------------


@pytest.mark.parametrize("role", ["underwriter", "admin", "Underwriter", " ADMIN "])
def test_officer_allowed_without_touching_db(monkeypatch, role):
    # An officer short-circuits before any DB lookup -- so the DB stub must never run.
    def _boom(*a, **k):
        raise AssertionError("officer path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_officer_or_owner(1, role, None)  # no raise


def test_owner_allowed(monkeypatch):
    # Borrower user 5 -> applicant 1; application 1 is owned by applicant 1 -> allowed.
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 1}, app_applicant_id=1)
    )
    authz.require_officer_or_owner(1, "borrower", "5")  # no raise


def test_non_owner_borrower_denied_404(monkeypatch):
    # Borrower user 5 -> applicant 1, but application 1 belongs to applicant 2.
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 1}, app_applicant_id=2)
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404  # no existence oracle: not 403


def test_anonymous_denied_without_db(monkeypatch):
    # No X-User-Id (anonymous /los caller): denied before any DB lookup.
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None)
    assert exc.value.status_code == 404


def test_unknown_user_denied(monkeypatch):
    # X-User-Id present but no such user row -> denied.
    monkeypatch.setattr(authz.db, "query", _authz_db(None, app_applicant_id=1))
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "999")
    assert exc.value.status_code == 404


def test_non_numeric_user_id_denied_without_db(monkeypatch):
    # A non-numeric X-User-Id can never match users.id (integer) -> fail closed, no query.
    def _boom(*a, **k):
        raise AssertionError("bad user id must not reach the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "not-a-number")
    assert exc.value.status_code == 404


def test_null_owner_application_denied_for_borrower(monkeypatch):
    # An anonymously-created application (applicant_id present but no owning user) cannot
    # be owned by any borrower -- so a borrower with a real applicant_id that happens not
    # to match is denied. Here the app's applicant_id is NULL (unlinked).
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 7}, app_applicant_id=None)
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404


def test_officer_user_with_null_applicant_still_allowed(monkeypatch):
    # Officers carry users.applicant_id = NULL; the role check must win regardless.
    def _boom(*a, **k):
        raise AssertionError("officer path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_officer_or_owner(1, "admin", "1")  # no raise


# --- continuation token (ADR 0010 Phase B: anonymous applicant, no login) -----


def test_valid_continuation_token_allowed(monkeypatch):
    # Anonymous applicant (no X-User-Id) holding this application's token is authorized.
    monkeypatch.setattr(
        authz.db, "query", _authz_db(None, app_applicant_id=None, app_token="tok-abc")
    )
    authz.require_officer_or_owner(
        1, None, None, x_application_token="tok-abc"
    )  # no raise


def test_wrong_continuation_token_denied(monkeypatch):
    monkeypatch.setattr(
        authz.db, "query", _authz_db(None, app_applicant_id=None, app_token="tok-real")
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="tok-wrong")
    assert exc.value.status_code == 404


def test_non_ascii_token_denied_404_not_500(monkeypatch):
    # A non-ASCII X-Application-Token against an existing token-bearing row must deny 404,
    # not 500: hmac.compare_digest raises TypeError on a non-ASCII str, and a 500-vs-404
    # split would leak which app_ids are real token-bearing applications (existence oracle).
    monkeypatch.setattr(
        authz.db, "query", _authz_db(None, app_applicant_id=None, app_token="tok-real")
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="tökén-🔑")
    assert exc.value.status_code == 404


def test_token_is_scoped_to_its_application(monkeypatch):
    # A token minted for application 1 must not authorize application 2: each application's
    # stored token is compared, so a token-for-1 fails the compare against app 2's token.
    def _q(sql, params=None):
        if "FROM applications" in sql:
            app_id = params[0]
            return [
                {
                    "applicant_id": None,
                    "continuation_token": authz.hash_token(f"token-for-{app_id}"),
                    "continuation_token_expires_at": _FUTURE,
                }
            ]
        return []

    monkeypatch.setattr(authz.db, "query", _q)
    authz.require_officer_or_owner(1, None, None, x_application_token="token-for-1")
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(2, None, None, x_application_token="token-for-1")
    assert exc.value.status_code == 404


def test_token_survives_service_token_rotation_with_dedicated_pepper(monkeypatch):
    # PR #7 review: the token hash is keyed by a DEDICATED pepper, not INTERNAL_SERVICE_TOKEN,
    # so rotating the service-auth secret does not invalidate live resume tokens.
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "v1:pepper-one")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "svc-A")
    stored = authz.hash_token("tok")
    monkeypatch.setattr(
        config, "INTERNAL_SERVICE_TOKEN", "svc-B-rotated"
    )  # rotate service
    assert (
        authz.verify_token("tok", stored) is True
    )  # pepper unchanged -> still verifies


def test_token_survives_pepper_rotation_until_old_key_dropped(monkeypatch):
    # Rotating the pepper itself: keep the old key configured for the grace window (<= TTL) so
    # pre-rotation tokens still verify; new tokens hash under the new current key; once the old
    # key is dropped, the old token no longer verifies.
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "v1:old")
    stored = authz.hash_token("tok")
    assert stored.startswith("v1:")
    monkeypatch.setattr(
        config, "CONTINUATION_TOKEN_KEYS", "v2:new,v1:old"
    )  # rotate w/ grace
    assert authz.verify_token("tok", stored) is True
    assert authz.hash_token("tok2").startswith("v2:")  # new tokens use the current key
    monkeypatch.setattr(
        config, "CONTINUATION_TOKEN_KEYS", "v2:new"
    )  # grace elapsed, drop v1
    assert authz.verify_token("tok", stored) is False


def test_hash_token_refuses_without_pepper_in_production(monkeypatch):
    # PR #7 review: a NEW token is NEVER hashed with the service secret. Outside development,
    # no dedicated pepper -> hash_token refuses (a production deploy is caught by
    # missing_required_secrets / RuntimeError, not silently coupled to INTERNAL_SERVICE_TOKEN).
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "")
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "svc-A")
    with pytest.raises(RuntimeError):
        authz.hash_token("tok")
    # and /health reports it missing
    assert "CONTINUATION_TOKEN_KEYS" in config.missing_required_secrets()


def test_intake_no_orphan_pii_when_token_hash_misconfigured(monkeypatch):
    # PR #7 review (blocking gate): the continuation-token/PII boundary. hash_token is computed
    # BEFORE any write, so a misconfigured pepper aborts submit with NOTHING persisted -- no
    # orphaned applicant PII row with no application, no token, and no app id for the gateway
    # compensator to target.
    from app import intake

    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "")
    monkeypatch.setattr(config, "ENVIRONMENT", "production")

    def _must_not_open(*a, **k):
        raise AssertionError(
            "no DB write may occur when the token hash cannot be computed"
        )

    monkeypatch.setattr(intake.db, "transaction", _must_not_open)
    with pytest.raises(RuntimeError):
        intake.create_application({"name": "T", "amount": 15000, "monthly_debt": 100})


def test_intake_writes_applicant_and_application_in_one_transaction(monkeypatch):
    # Atomicity: applicant + application are inserted in a SINGLE transaction (applicant first),
    # so a failure on the application insert rolls back the applicant -- no orphaned PII.
    from contextlib import contextmanager

    from app import intake

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql.strip())

        def fetchone(self):
            return {"id": 1}

    @contextmanager
    def _txn():
        yield _Cur()

    monkeypatch.setattr(intake.db, "transaction", _txn)
    # No stray db.query writes outside the transaction.
    monkeypatch.setattr(
        intake.db,
        "query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no autocommit write")),
    )
    intake.create_application({"name": "T", "amount": 15000, "monthly_debt": 100})
    inserts = [
        s.split("INSERT INTO ")[1].split()[0] for s in executed if "INSERT INTO" in s
    ]
    assert inserts == ["applicants", "applications"]


def test_hash_token_dev_fallback_is_service_token_coupled(monkeypatch):
    # Development-only convenience: no dedicated pepper -> hash under INTERNAL_SERVICE_TOKEN so
    # the local demo runs. Prove it is still service-token-coupled (breaks on service rotation)
    # -- exactly why production must set CONTINUATION_TOKEN_KEYS. In dev this is NOT reported
    # missing (the relaxation), but a rotated service token breaks the dev token.
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "svc-A")
    stored = authz.hash_token("tok")
    assert stored.startswith("legacy:")
    assert authz.verify_token("tok", stored) is True
    assert "CONTINUATION_TOKEN_KEYS" not in config.missing_required_secrets()
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "svc-B")
    assert authz.verify_token("tok", stored) is False


def test_verify_accepts_preexisting_legacy_row_after_pepper_set(monkeypatch):
    # verify-only fallback: a row hashed under the old service-token coupling ("legacy:...")
    # must STILL verify once a dedicated pepper is configured, so rotating IN the pepper does
    # not strand pre-existing tokens. New tokens hash under the pepper; old rows verify via the
    # service-token legacy key until they expire.
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "svc-A")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "")
    legacy_stored = authz.hash_token("tok")  # hashed under svc-A as "legacy:"
    assert legacy_stored.startswith("legacy:")
    # Operator now decouples: sets a dedicated pepper. New tokens use it...
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "v1:pepper")
    assert authz.hash_token("tok2").startswith("v1:")
    # ...and the pre-existing legacy row still verifies (service token still configured).
    assert authz.verify_token("tok", legacy_stored) is True


def test_configured_legacy_version_is_reserved(monkeypatch):
    # PR #7 review: "legacy" is reserved for the verify-only service-token fallback. A configured
    # key that claims it is ignored (two secrets for one version would break rotation). Here the
    # ONLY key is named "legacy" -> parsed set is empty -> hash_token refuses in production, the
    # intended loud nudge to rename, rather than silently colliding with the fallback.
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "legacy:should-be-ignored")
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    assert config.continuation_token_keys() == []
    with pytest.raises(RuntimeError):
        authz.hash_token("tok")
    # A real key alongside a (reserved) "legacy" entry keeps only the real one.
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "v1:real,legacy:ignored")
    assert config.continuation_token_keys() == [("v1", "real")]


def test_abandon_requires_internal_token(monkeypatch):
    # Compensating /abandon is reachable through the anonymous /los proxy, so it is
    # internal-only (the gateway strips any client X-Internal-Service). No token -> 403.
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "sekret")
    resp = TestClient(app).post("/applications/5/abandon")
    assert resp.status_code == 403


class _FakeTxCursor:
    """Records executes from the abandon transaction; returns "no other applications" from the
    applicant-reference probe so the applicant (and its dependent rows) are deleted."""

    def __init__(self, other_apps=False):
        self.executed = []
        self._other_apps = other_apps
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))
        if "SELECT 1 FROM applications WHERE applicant_id" in sql:
            self._last = [object()] if self._other_apps else []
        else:
            self._last = None

    def fetchone(self):
        return self._last[0] if self._last else None

    def deletes(self):
        return [
            (
                sql.split()[2].lower(),
                params,
            )  # ("applications"/"kyc_checks"/"applicants", params)
            for sql, params in self.executed
            if sql.upper().startswith("DELETE FROM")
        ]


def _fake_transaction_factory(cur):
    class _CM:
        def __enter__(self):
            return cur

        def __exit__(self, *exc):
            return False

    return lambda: _CM()


def test_abandon_deletes_inert_application(monkeypatch):
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "sekret")

    def _q(sql, params=None):
        if sql.strip().upper().startswith("SELECT A.APPLICANT_ID"):  # inertness probe
            return [{"applicant_id": 42, "n_decisions": 0, "n_offers": 0, "n_loans": 0}]
        raise AssertionError(f"unexpected query: {sql}")

    cur = _FakeTxCursor(other_apps=False)
    monkeypatch.setattr(applications.db, "query", _q)
    monkeypatch.setattr(applications.db, "transaction", _fake_transaction_factory(cur))
    resp = TestClient(app).post(
        "/applications/5/abandon", headers={"X-Internal-Service": "sekret"}
    )
    assert resp.status_code == 200
    deletes = cur.deletes()
    assert ("applications", (5,)) in deletes
    assert ("applicants", (42,)) in deletes


def test_abandon_deletes_dependent_kyc_rows_before_applicant(monkeypatch):
    # PR #7 review regression: submit runs KYC before the resume session is stored, so an
    # abandoned application usually has a kyc_checks row keyed to applicant_id. Its FK has no
    # cascade, so the compensation MUST delete kyc_checks before the applicant -- else the
    # applicant delete FK-fails and the applicant + KYC/PII are stranded. Assert the order.
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "sekret")

    def _q(sql, params=None):
        if sql.strip().upper().startswith("SELECT A.APPLICANT_ID"):
            return [{"applicant_id": 42, "n_decisions": 0, "n_offers": 0, "n_loans": 0}]
        raise AssertionError(f"unexpected query: {sql}")

    cur = _FakeTxCursor(other_apps=False)
    monkeypatch.setattr(applications.db, "query", _q)
    monkeypatch.setattr(applications.db, "transaction", _fake_transaction_factory(cur))
    resp = TestClient(app).post(
        "/applications/5/abandon", headers={"X-Internal-Service": "sekret"}
    )
    assert resp.status_code == 200
    deletes = cur.deletes()
    tables = [t for t, _ in deletes]
    assert ("kyc_checks", (42,)) in deletes
    # kyc_checks (child) must be deleted before applicants (parent).
    assert tables.index("kyc_checks") < tables.index("applicants")


def test_abandon_keeps_applicant_with_other_applications(monkeypatch):
    # If another application references the applicant, only the application is deleted -- the
    # shared applicant (and its KYC rows) stay. Guards against deleting a still-referenced row.
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "sekret")

    def _q(sql, params=None):
        if sql.strip().upper().startswith("SELECT A.APPLICANT_ID"):
            return [{"applicant_id": 42, "n_decisions": 0, "n_offers": 0, "n_loans": 0}]
        raise AssertionError(f"unexpected query: {sql}")

    cur = _FakeTxCursor(other_apps=True)
    monkeypatch.setattr(applications.db, "query", _q)
    monkeypatch.setattr(applications.db, "transaction", _fake_transaction_factory(cur))
    resp = TestClient(app).post(
        "/applications/5/abandon", headers={"X-Internal-Service": "sekret"}
    )
    assert resp.status_code == 200
    tables = [t for t, _ in cur.deletes()]
    assert tables == ["applications"]  # applicant + kyc_checks untouched


def test_abandon_raced_non_inert_delete_becomes_409(monkeypatch):
    # PR #7 review (TOCTOU): the inertness probe runs outside the transaction, so a decision
    # could race in between the probe and the DELETE. The RESTRICT FK (decisions/decision_events
    # -> applications, no cascade) makes the DELETE raise ForeignKeyViolation rather than
    # wrongly removing a non-inert application; the handler must surface that as 409, not 500.
    from psycopg2 import errors as pg_errors

    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "sekret")

    def _q(sql, params=None):
        if sql.strip().upper().startswith("SELECT A.APPLICANT_ID"):
            return [{"applicant_id": 42, "n_decisions": 0, "n_offers": 0, "n_loans": 0}]
        raise AssertionError(f"unexpected query: {sql}")

    class _RacingCursor:
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("DELETE FROM APPLICATIONS"):
                raise pg_errors.ForeignKeyViolation("decisions_app_id_fkey")

        def fetchone(self):
            return None

    monkeypatch.setattr(applications.db, "query", _q)
    monkeypatch.setattr(
        applications.db, "transaction", _fake_transaction_factory(_RacingCursor())
    )
    resp = TestClient(app).post(
        "/applications/5/abandon", headers={"X-Internal-Service": "sekret"}
    )
    assert resp.status_code == 409


def test_abandon_refuses_non_inert_application(monkeypatch):
    # An application with a decision/offer/loan is past the submit window -> never deleted.
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "sekret")

    def _q(sql, params=None):
        if sql.strip().upper().startswith("SELECT A.APPLICANT_ID"):
            return [{"applicant_id": 42, "n_decisions": 1, "n_offers": 0, "n_loans": 0}]
        raise AssertionError("must not delete a non-inert application")

    monkeypatch.setattr(applications.db, "query", _q)
    resp = TestClient(app).post(
        "/applications/5/abandon", headers={"X-Internal-Service": "sekret"}
    )
    assert resp.status_code == 409


def test_token_against_null_token_row_denied(monkeypatch):
    # An officer-created / legacy row has no continuation_token -> the token path is closed
    # (a caller cannot supply a matching token for a NULL).
    monkeypatch.setattr(
        authz.db,
        "query",
        _authz_db(None, app_applicant_id=9, app_token=None, app_exists=True),
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="anything")
    assert exc.value.status_code == 404


# --- token hardening (PR #7 review): hash-at-rest, expiry, single-use-at-funding ----


def test_token_stored_as_hash_not_raw(monkeypatch):
    # hash-at-rest: authz compares hash(presented) against the stored digest, so a DB that
    # leaked the STORED value cannot be replayed. Model a (pre-hardening) row that stored the
    # RAW token: presenting that exact raw value must NOT authorize, because authz hashes it
    # first and the hash != the stored raw string.
    raw = "leaked-raw-token"

    def _q(sql, params=None):
        if "FROM applications" in sql:
            return [
                {
                    "applicant_id": None,
                    "continuation_token": raw,  # stored in the clear (leaked/legacy)
                    "continuation_token_expires_at": _FUTURE,
                }
            ]
        return []

    monkeypatch.setattr(authz.db, "query", _q)
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token=raw)
    assert exc.value.status_code == 404
    # sanity: the SAME raw token DOES authorize a row that stored its hash (the real path).
    monkeypatch.setattr(
        authz.db, "query", _authz_db(None, app_applicant_id=None, app_token=raw)
    )
    authz.require_officer_or_owner(1, None, None, x_application_token=raw)  # no raise


def test_expired_token_denied(monkeypatch):
    # A valid token past its expiry is no longer a capability -> 404 (same as wrong token,
    # no existence oracle).
    monkeypatch.setattr(
        authz.db,
        "query",
        _authz_db(None, app_applicant_id=None, app_token="tok-abc", expires_at=_PAST),
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="tok-abc")
    assert exc.value.status_code == 404


def test_null_expiry_token_denied(monkeypatch):
    # A token row with a NULL expiry (a pre-hardening row, or one never stamped) fails
    # closed on the token path -- only a freshly issued, unexpired token authorizes.
    monkeypatch.setattr(
        authz.db,
        "query",
        _authz_db(None, app_applicant_id=None, app_token="tok-abc", expires_at=None),
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="tok-abc")
    assert exc.value.status_code == 404


def test_accept_retires_token_expiry_but_preserves_hash_on_funding(monkeypatch):
    # single-use at the money action: boarding retires the bearer token for every forward
    # route by nulling its EXPIRY (authz treats a NULL expiry as expired), so it can no
    # longer re-drive a funded application. The token HASH is deliberately PRESERVED so a
    # lost-response accept retry can still be verified for the replay-only recovery path
    # (see test_anonymous_accept_retry_replays_loan_after_token_retired). Assert the funded
    # UPDATE nulls the expiry and does NOT null the hash.
    captured = []

    def _q(sql, params=None):
        s = sql.strip().upper()
        captured.append(s)
        if "LEFT JOIN KYC_CHECKS" in s:  # kyc gate -> passing natural person
            return [
                {
                    "is_entity": False,
                    "name_verified": True,
                    "dob_verified": True,
                    "address_verified": True,
                    "ssn_verified": True,
                }
            ]
        if "FROM APPLICATIONS A" in s:  # approve + offer row
            return [
                {
                    "amount": 15000,
                    "term_months": 36,
                    "name": "Jane",
                    "apr": 12.5,
                    "outcome": "approve",
                }
            ]
        if "FROM LOANS WHERE APP_ID" in s:  # already boarded -> skip board_to_servicing
            return [{"id": 77, "principal": 15000}]
        return []

    monkeypatch.setattr(applications.db, "query", _q)
    resp = TestClient(app).post(
        "/applications/1/accept", headers={"X-User-Role": "underwriter"}
    )
    assert resp.status_code == 200
    funded = next(s for s in captured if s.startswith("UPDATE APPLICATIONS SET STATUS"))
    assert "CONTINUATION_TOKEN_EXPIRES_AT = NULL" in funded
    # the hash must survive funding so the terminal accept-retry can still verify it
    assert "CONTINUATION_TOKEN = NULL" not in funded


def test_anonymous_accept_retry_replays_loan_after_token_retired(monkeypatch):
    # PR review regression: the FIRST anonymous accept funds the loan and retires the token
    # (expiry nulled, hash preserved). The applicant's browser loses the response and
    # retries with the SAME token. The token now fails the normal (expired) authz check, so
    # the retry must fall through to terminal_accept_replay and return the SAME loan -- an
    # idempotent success -- never a 404 that hides the funded loan. Exercises the token
    # path, not officer replay.
    stored_hash = authz.hash_token(_E2E_TOKEN)

    def _q(sql, params=None):
        s = sql.strip().upper()
        # normal authz lookup: token retired -> expiry is NULL -> _expired() denies
        if "CONTINUATION_TOKEN_EXPIRES_AT FROM APPLICATIONS" in s:
            return [
                {
                    "applicant_id": None,
                    "continuation_token": stored_hash,
                    "continuation_token_expires_at": None,
                }
            ]
        # terminal_accept_replay: funded app still carries the preserved hash
        if "SELECT CONTINUATION_TOKEN FROM APPLICATIONS" in s:
            return [{"continuation_token": stored_hash}]
        # terminal_accept_replay: the already-boarded loan
        if "SELECT ID FROM LOANS WHERE APP_ID" in s:
            return [{"id": 909}]
        raise AssertionError(f"unexpected query on retry path: {sql}")

    monkeypatch.setattr(applications.db, "query", _q)
    monkeypatch.setattr(authz.db, "query", _q)

    def _must_not_board(*a, **k):
        raise AssertionError("terminal replay must not board a second loan")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _must_not_board)

    resp = TestClient(app).post(
        "/applications/1/accept",
        headers={"X-Application-Token": _E2E_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 909  # same funded loan, replayed idempotently


def test_retired_token_still_denied_on_forward_route(monkeypatch):
    # Security regression: preserving the token hash at funding must NOT reopen forward
    # routes. A retired token (expiry NULL, hash present) presented to decision must still
    # 404 -- the replay allowance is accept-scoped, not a general capability.
    monkeypatch.setattr(
        authz.db,
        "query",
        _authz_db(None, app_applicant_id=None, app_token=_E2E_TOKEN, expires_at=None),
    )
    resp = TestClient(app).post(
        "/applications/1/decision",
        headers={"X-Application-Token": _E2E_TOKEN},
    )
    assert resp.status_code == 404


def test_owner_still_allowed_with_token_column_present(monkeypatch):
    # Regression: adding the token path must not break the owner path. Owner matches even
    # with a token column present and no token supplied.
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 1}, app_applicant_id=1)
    )
    authz.require_officer_or_owner(1, "borrower", "5")  # no raise


# --- require_officer (list / assistant) ---------------------------------------


def test_require_officer_allows_officer():
    authz.require_officer("underwriter")  # no raise


@pytest.mark.parametrize("role", ["borrower", "csr", None, ""])
def test_require_officer_denies_non_officer(role):
    with pytest.raises(HTTPException) as exc:
        authz.require_officer(role)
    assert exc.value.status_code == 403


# --- full-stack wiring: anonymous is denied on every application-scoped route -


def test_anonymous_accept_denied_through_route():
    # authz runs first, so an anonymous accept is denied before any boarding DB work.
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 404


def test_anonymous_offer_denied_through_route():
    resp = TestClient(app).post("/offer", json={"app_id": 1})
    assert resp.status_code == 404


def test_anonymous_decision_denied_through_route():
    resp = TestClient(app).post("/applications/1/decision")
    assert resp.status_code == 404


def test_anonymous_recheck_kyc_denied_through_route():
    # recheck-kyc re-runs a regulated identity check + persists a kyc_checks row, so it is
    # application-scoped like decision/offer/accept: authz runs first, an anonymous caller
    # is denied 404 before any applicant PII is loaded or KYC is re-run.
    resp = TestClient(app).post("/applications/1/recheck-kyc")
    assert resp.status_code == 404


def test_anonymous_list_denied_through_route():
    # The roster is officer-only (PII dump) -> 403 for an anonymous caller. Override the
    # DB session dependency (resolved before the handler body) so the test exercises the
    # authz gate, not the test env's absent database.
    app.dependency_overrides[get_session] = lambda: None
    try:
        resp = TestClient(app).get("/applications")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


# --- end-to-end: the public (logged-out) apply flow works via the token --------


_E2E_TOKEN = "e2e-continuation-token"


def _apply_flow_db(state):
    """Stateful db.query for the public apply flow: the authz lookup returns the token that
    create_application issued (the token is now persisted in the application INSERT, which
    is stubbed, so the lookup returns the known fixed token); decision_request_payload
    returns a decisionable row. Models an owner-less (anonymous) application authorized
    purely by its token."""

    def _q(sql, params=None):
        s = sql.strip().upper()
        if "CONTINUATION_TOKEN_EXPIRES_AT FROM APPLICATIONS" in s:  # authz lookup
            return [
                {
                    "applicant_id": None,
                    "continuation_token": authz.hash_token(_E2E_TOKEN),
                    "continuation_token_expires_at": _FUTURE,
                }
            ]
        if "LEFT JOIN KYC_CHECKS" in s:  # ADR 0011 KYC gate -> passing (natural person)
            return [
                {
                    "is_entity": False,
                    "name_verified": True,
                    "dob_verified": True,
                    "address_verified": True,
                    "ssn_verified": True,
                }
            ]
        if s.startswith("SELECT APPLICANT_ID FROM APPLICATIONS"):  # submit resolve
            return [{"applicant_id": None}]
        if "APPLICATIONS A JOIN APPLICANTS" in s:  # recheck applicant-load
            return [
                {
                    "applicant_id": None,
                    "name": "Jane",
                    "dob": None,
                    "ssn": None,
                    "address": "10 Main St",
                    "is_entity": False,
                }
            ]
        if "LEFT JOIN APPLICANTS" in s:  # decision_request_payload
            return [
                {
                    "id": 1,
                    "applicant_id": None,
                    "amount": 15000,
                    "term_months": 36,
                    "income": 50000,
                    "monthly_debt": 500,
                    "employment_years": 3,
                    "name": "Jane",
                    "ssn": "123456789",
                }
            ]
        return []

    return _q


def _apply_flow_clients_post(base, path, payload):
    if "/kyc" in path:
        return {"cip_passed": True}
    if "/decision" in path:
        return {"outcome": "approve", "score": 700, "reason": None}
    raise AssertionError(f"unexpected downstream call: {path}")


def test_public_apply_flow_completes_with_continuation_token(monkeypatch):
    # e2e for the logged-out applicant (the reviewer's ask): submit returns a token, and a
    # decision carrying that token as X-Application-Token succeeds -- while the same
    # decision with NO token is denied 404. Proves anonymous apply still works end to end
    # without a login, and only with the scoped capability.
    state = {"token": None}
    monkeypatch.setattr(
        applications.intake, "create_application", lambda payload: (1, _E2E_TOKEN)
    )
    monkeypatch.setattr(applications.db, "query", _apply_flow_db(state))
    monkeypatch.setattr(applications.clients, "post", _apply_flow_clients_post)
    client = TestClient(app)

    submitted = client.post(
        "/applications",
        json={"name": "Jane", "amount": 15000, "monthly_debt": 500},
    )
    assert submitted.status_code == 200
    token = submitted.json()["continuation_token"]
    assert token == _E2E_TOKEN  # issued at submit and returned once

    # No token -> denied (anonymous, no capability).
    denied = client.post("/applications/1/decision")
    assert denied.status_code == 404

    # With the issued token -> authorized, decision returns.
    ok = client.post("/applications/1/decision", headers={"X-Application-Token": token})
    assert ok.status_code == 200
    assert ok.json()["decision"] == "approve"


def test_recheck_preserves_token_for_anonymous_recovery(monkeypatch):
    # PR review: a token-authenticated recheck must echo the capability back, or a client
    # that updates its state from the ApplicationCreated response would null its own token
    # and be unable to proceed. Prove the token survives recheck AND still authorizes the
    # next call using only the response-updated state.
    monkeypatch.setattr(applications.db, "query", _apply_flow_db({}))
    monkeypatch.setattr(applications.clients, "post", _apply_flow_clients_post)
    client = TestClient(app)

    rechecked = client.post(
        "/applications/1/recheck-kyc",
        headers={"X-Application-Token": _E2E_TOKEN},
    )
    assert rechecked.status_code == 200
    # the recovered client reads its capability from this response, as it does at submit
    token = rechecked.json()["continuation_token"]
    assert token == _E2E_TOKEN

    # that response-carried token still authorizes the next step (no resubmit, no support).
    ok = client.post("/applications/1/decision", headers={"X-Application-Token": token})
    assert ok.status_code == 200
    assert ok.json()["decision"] == "approve"
