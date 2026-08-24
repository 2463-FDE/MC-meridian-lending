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
  - no money path writes any audit or ledger row, EXCEPT apply_payment, which since
    D3 (ADR 0020) writes payment_applications in the same statement as the movement

ADR 0020 landed and changed three of these deliberately, which is the signal this
file exists to produce: apply_payment is now one atomic statement, it now records the
movement, and adjust_balance/waive_fee are the two that keep the unlocked shape.

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
            # One eligible captured payment (loan 1, $100.00) and an empty
            # payment_applications, so the D3 apply has something real to act on.
            self.payments = {
                7: {"loan_id": 1, "amount_minor": 10000, "status": "captured"}
            }
            self.applications = {}

        def query(self, sql, params=None):
            self.statements.append((" ".join(sql.split()), params))
            upper = sql.upper()
            # The D3 apply: one statement that records the application and moves the
            # balance together. Modelled by outcome rather than by parsing the SQL --
            # eligible and unapplied moves the money, anything else returns no rows.
            if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("WITH"):
                payment = self.payments.get(params["payment_id"])
                if (
                    payment is None
                    or payment["loan_id"] != params["loan_id"]
                    or payment["amount_minor"] is None
                    or payment["status"] not in ("captured", "settled")
                    or params["payment_id"] in self.applications
                ):
                    return []
                self.applications[params["payment_id"]] = {
                    "loan_id": payment["loan_id"],
                    "amount_minor": payment["amount_minor"],
                }
                self.row["balance"] -= payment["amount_minor"] / 100.0
                self.row["updated_at"] = "t1"
                return [
                    {
                        "loan_id": payment["loan_id"],
                        "balance": self.row["balance"],
                        "amount_minor": payment["amount_minor"],
                    }
                ]
            if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("SELECT"):
                prior = self.applications.get(params[0])
                return [prior] if prior else []
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
    new_balance, moved = balance.apply_payment(1, 7)

    assert moved is True
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


# apply_payment is deliberately absent: since D3 it DOES write a ledger row, which is
# the next test. The three below still record nothing anywhere.
@pytest.mark.parametrize(
    "move",
    [
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


def test_apply_payment_now_records_the_movement(db_spy):
    # The one money path that stopped being unrecorded (D3 / ADR 0020). This is the
    # ledger seam: the application row is the event, and it is written by the same
    # statement that moves the balance.
    balance.apply_payment(1, 7)

    sql = db_spy.sql_text().upper()
    assert "INSERT INTO PAYMENT_APPLICATIONS" in sql, db_spy.statements
    assert db_spy.applications[7] == {"loan_id": 1, "amount_minor": 10000}


def test_apply_payment_is_one_atomic_statement(db_spy):
    # The inverse of what this file pinned before D3: the read-modify-write is gone,
    # the decrement computes from the stored value inside the UPDATE, and the record
    # and the movement are the same statement.
    balance.apply_payment(1, 7)

    sql = db_spy.sql_text().upper()
    assert len(db_spy.statements) == 1, db_spy.statements
    assert "SET BALANCE = B.BALANCE -" in sql, (
        "the decrement must compute from the stored value inside the statement"
    )


def test_the_other_mutations_are_still_a_separate_unlocked_read_then_write(db_spy):
    # adjust_balance and waive_fee keep the shape D3 removed from apply_payment: two
    # statements, no transaction, no row lock. A different defect on a different
    # column, carded in docs/debt-log.md rather than fixed by ADR 0020.
    balance.adjust_balance(1, 100.0)
    balance.waive_fee(1, 10.0)

    sql = db_spy.sql_text().upper()
    assert len(db_spy.statements) == 4, db_spy.statements
    assert "FOR UPDATE" not in sql
    assert "BEGIN" not in sql
    assert "SET PAST_DUE = PAST_DUE" not in sql, (
        "an atomic decrement would not read first"
    )
