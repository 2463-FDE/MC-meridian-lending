"""Amortization-schedule tests (these PASS)."""

from app import schedule


def test_schedule_length_matches_term():
    rows = schedule.amortization(15000, 7.142, 36)
    assert len(rows) == 36


def test_schedule_amortizes_to_zero():
    rows = schedule.amortization(20000, 10.0, 60)
    assert rows[-1]["balance"] == 0.0


def test_monthly_payment_positive():
    assert schedule.monthly_payment(10000, 8.5, 48) > 0


def test_loan_schedule_route_amortizes_at_note_rate_not_apr():
    # PR review: the schedule must amortize at the loan's NOTE rate, not the disclosed APR.
    # The APR carries the prepaid origination fee and is higher, so a schedule built at it
    # contradicts the loan's own TILA disclosure. Call the handler directly with a stub loan
    # (no DB), mirroring test_db_readiness.py.
    from app.routers import loans as loans_router

    class _Loan:
        principal = 17460.0
        apr = 9.584  # disclosed APR (higher — carries the fee)
        note_rate = 7.99  # contractual rate the schedule must use
        term_months = 48

    class _Session:
        def get(self, model, pk):
            return _Loan()

    out = loans_router.loan_schedule(1, session=_Session())
    first_interest = out.schedule[0].interest
    # First-period interest is balance * (rate/1200). It must equal the note-rate figure,
    # never the APR figure.
    assert first_interest == round(17460.0 * (7.99 / 100 / 12), 2)
    assert first_interest != round(17460.0 * (9.584 / 100 / 12), 2)


def test_loan_schedule_route_falls_back_to_apr_for_legacy_loan():
    # A loan boarded before note_rate existed has note_rate=None; the schedule falls back to
    # apr, preserving pre-change behavior for those rows.
    from app.routers import loans as loans_router

    class _Loan:
        principal = 15000.0
        apr = 8.5
        note_rate = None
        term_months = 36

    class _Session:
        def get(self, model, pk):
            return _Loan()

    out = loans_router.loan_schedule(1, session=_Session())
    assert out.schedule[0].interest == round(15000.0 * (8.5 / 100 / 12), 2)
