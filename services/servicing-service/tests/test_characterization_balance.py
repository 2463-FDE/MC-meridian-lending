"""Characterization (golden-master) tests for the servicing money surface.

These pin what the code does TODAY, not what it should do. They exist so the fixes
specified in ADR 0013 (atomic apply) and ADR 0014 (authorization, ledger) have to
change them deliberately rather than silently — a failing test here after one of
those lands is the intended signal, and the fix's own PR updates it.

What is pinned:
  - adjust_balance overwrites the figure in place; the prior value is not recorded
  - waive_fee reduces past_due only, never balance
  - apply_payment reduces balance only, never past_due
  - assess_late_fee adds a flat $35 to past_due and does not touch updated_at
  - the x_user_role header on adjust-balance / waive-fee is accepted and IGNORED
  - no money path writes any audit or ledger row

Money here is float because the code is float (D2); the assertions use pytest.approx
so they pin behaviour rather than re-testing float representation.
"""

import pytest

from app import balance, delinquency, main


@pytest.fixture
def db_spy(monkeypatch):
    """Stand in for app.db.query over a one-row in-memory `balances` table.

    The real module holds a module-level psycopg2 connection with autocommit on
    (app/db.py:9-14); the tests in this service stub at the module boundary rather
    than reaching a database, so this mirrors that idiom. Every statement is
    recorded so a test can assert what was and was not written.
    """

    class Spy:
        def __init__(self):
            self.row = {"balance": 500.0, "past_due": 75.0, "updated_at": "t0"}
            self.statements = []

        def query(self, sql, params=None):
            self.statements.append((" ".join(sql.split()), params))
            upper = sql.upper()
            if upper.strip().startswith("SELECT"):
                # Match the select LIST, not the whole statement: "FROM balances"
                # contains the substring BALANCE and would swallow every SELECT.
                selected = upper.split("FROM")[0]
                if "PAST_DUE" in selected:
                    return [{"past_due": self.row["past_due"]}]
                if "BALANCE" in selected:
                    return [{"balance": self.row["balance"]}]
                return []
            if "UPDATE BALANCES" in upper:
                if "SET BALANCE" in upper:
                    self.row["balance"] = params[0]
                elif "SET PAST_DUE" in upper:
                    self.row["past_due"] = params[0]
                if "UPDATED_AT" in upper:
                    self.row["updated_at"] = "t1"
                return []
            return []

        def sql_text(self):
            return " ".join(s for s, _ in self.statements)

    spy = Spy()
    # balance.py and delinquency.py each import the db module, so patch the shared module.
    monkeypatch.setattr(balance.db, "query", spy.query)
    return spy


# --- what each money path actually mutates ---------------------------------


def test_apply_payment_subtracts_from_balance_and_leaves_past_due(db_spy):
    new_balance = balance.apply_payment(1, 100.0)

    assert new_balance == pytest.approx(400.0)
    assert db_spy.row["balance"] == pytest.approx(400.0)
    assert db_spy.row["past_due"] == pytest.approx(75.0), (
        "apply_payment must not touch past_due"
    )


def test_adjust_balance_overwrites_in_place_and_loses_the_prior_value(db_spy):
    returned = balance.adjust_balance(1, 12.34)

    assert returned == pytest.approx(12.34)
    assert db_spy.row["balance"] == pytest.approx(12.34)
    # The prior 500.0 is not written anywhere: the only statements are one SELECT and
    # one UPDATE of the same column. This is the whole of Q3 in the week-6 report.
    assert len(db_spy.statements) == 2, db_spy.statements
    assert "500" not in db_spy.sql_text()
    assert not any(p and 500.0 in p for _, p in db_spy.statements)


def test_waive_fee_subtracts_from_past_due_and_leaves_balance(db_spy):
    new_past_due = balance.waive_fee(1, 25.0)

    assert new_past_due == pytest.approx(50.0)
    assert db_spy.row["past_due"] == pytest.approx(50.0)
    # Different column from apply_payment — which is why the client brief's
    # payment-plus-waiver pair does NOT reproduce a lost update (report Q2).
    assert db_spy.row["balance"] == pytest.approx(500.0), (
        "waive_fee must not touch balance"
    )


def test_waive_fee_accepts_an_amount_larger_than_past_due(db_spy):
    # No validation: past_due goes negative. Pinned as current behaviour, not endorsed.
    assert balance.waive_fee(1, 1_000.0) == pytest.approx(-925.0)


def test_late_fee_adds_flat_35_and_does_not_touch_updated_at(db_spy):
    monkey_before = db_spy.row["updated_at"]

    new_past_due = delinquency.assess_late_fee(1)

    assert new_past_due == pytest.approx(110.0)
    # $35 flat, though the policy says "$35 OR 5% of past due, whichever is less"
    # (delinquency.py:13). And unlike every other mutation, this one leaves the
    # timestamp alone, so a late fee changes the figure with no trace at all.
    assert db_spy.row["updated_at"] == monkey_before
    assert "UPDATED_AT" not in db_spy.sql_text().upper()


# --- the role header is declared and ignored -------------------------------


@pytest.mark.parametrize(
    "role", ["borrower", "csr", "underwriter", "admin", None, "", "nonsense"]
)
def test_adjust_balance_route_ignores_x_user_role(db_spy, role):
    # main.py:103 declares x_user_role and never reads it, so every role — including
    # a borrower and an unrecognized string — moves money. Closing this is ADR 0014
    # Decision 1, and this test is expected to change when that lands.
    out = main.adjust_balance(1, main.AdjustIn(new_balance=0.0), x_user_role=role)

    assert out == {"loan_id": 1, "balance": pytest.approx(0.0)}
    assert db_spy.row["balance"] == pytest.approx(0.0)


@pytest.mark.parametrize("role", ["borrower", "csr", None])
def test_waive_fee_route_ignores_x_user_role(db_spy, role):
    out = main.waive_fee(1, main.WaiveIn(amount=75.0), x_user_role=role)

    assert out == {"loan_id": 1, "past_due": pytest.approx(0.0)}


def test_apply_payment_route_takes_no_caller_identity_at_all():
    # Not even a header to ignore: the signature is (loan_id, body). ADR 0013 makes
    # this internal-service only, which changes this signature.
    import inspect

    assert list(inspect.signature(main.apply_payment).parameters) == ["loan_id", "body"]


def test_late_fee_route_takes_no_caller_identity_at_all():
    import inspect

    assert list(inspect.signature(main.late_fee).parameters) == ["loan_id"]


# --- nothing records the movement -----------------------------------------


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(lambda: balance.apply_payment(1, 10.0), id="apply_payment"),
        pytest.param(lambda: balance.adjust_balance(1, 10.0), id="adjust_balance"),
        pytest.param(lambda: balance.waive_fee(1, 10.0), id="waive_fee"),
        pytest.param(lambda: delinquency.assess_late_fee(1), id="late_fee"),
    ],
)
def test_no_money_path_writes_an_audit_or_ledger_row(db_spy, move):
    move()

    sql = db_spy.sql_text().upper()
    for table in (
        "AUDIT_LOGS",
        "DECISION_EVENTS",
        "BALANCE_POSTINGS",
        "PAYMENT_APPLICATIONS",
    ):
        assert table not in sql, f"{table} written unexpectedly: {db_spy.statements}"
    assert "INSERT" not in sql, f"unexpected INSERT: {db_spy.statements}"


def test_every_mutation_is_a_separate_unlocked_read_then_write(db_spy):
    # The shape behind D3: two statements, no transaction, no row lock. If a fix
    # makes this one atomic statement, this test is what says so out loud.
    balance.apply_payment(1, 100.0)

    sql = db_spy.sql_text().upper()
    assert len(db_spy.statements) == 2, db_spy.statements
    assert "FOR UPDATE" not in sql
    assert "BEGIN" not in sql
    assert "SET BALANCE = BALANCE" not in sql, (
        "an atomic decrement would not read first"
    )
