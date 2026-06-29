"""PAN-masking tests for the display helper (these PASS).

NOTE: masking is a display-only helper. The payments TABLE still stores the full PAN and
CVV, and the payment LOG still writes them in the clear — neither is covered by a test
(deliberate coverage gap: the PCI debt is not guarded anywhere).

There is deliberately NO test for payment idempotency either — a retried POST writes a
second payments row (double-charge, D2) and that debt stays untested on purpose.
"""
from app.routers.payments import _mask_pan


def test_mask_pan_shows_last_four():
    assert _mask_pan("4111111111111111") == "•••• 1111"


def test_mask_pan_handles_none():
    assert _mask_pan(None) is None
    assert _mask_pan("") is None
