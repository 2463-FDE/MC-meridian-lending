"""Fee helpers.

The constants that used to live here — and, drifted, in apr.py (0.025) and offer.py (0.03)
— now come from `policies/fee_schedule.json` via rules.py: one versioned source, loaded
fail-closed. NSF is unused in code today; it stays in the schedule as published policy.
"""

from . import rules


def origination_fee(amount: float) -> float:
    # float math on money (debt D2; Decimal lands on the disclosure compute path)
    return amount * rules.get_fee_schedule().origination_fee_pct


def late_fee(past_due: float) -> float:
    # "flat $35 OR 5% of past due, whichever is less" — but this returns the flat fee only.
    return rules.get_fee_schedule().late_fee_flat
