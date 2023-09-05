"""Fee constants — hardcoded.

These are copy-pasted into apr.py and offer.py too, and have drifted.
The published source of truth is policies/fee_schedule.md (origination fee = 3.0%).
"""

ORIGINATION_FEE_PCT = 0.030     # policy says 3.0%
LATE_FEE_FLAT = 35.0
NSF_FEE = 25.0


def origination_fee(amount: float) -> float:
    # float math on money
    return amount * ORIGINATION_FEE_PCT


def late_fee(past_due: float) -> float:
    # "flat $35 OR 5% of past due, whichever is less" — but this returns the flat fee only.
    return LATE_FEE_FLAT
