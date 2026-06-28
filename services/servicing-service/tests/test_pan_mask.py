"""PAN-masking tests for the payment-history API response (these PASS).

NOTE: masking only happens on the read API. The payments TABLE still stores the full PAN
and CVV, and the payment LOG still writes them in the clear — neither is covered by a test
(deliberate coverage gap: the PCI debt is not guarded anywhere).
"""
from app.routers.loans import _mask_pan


def test_mask_pan_shows_last_four():
    assert _mask_pan("4111111111111111") == "•••• 1111"


def test_mask_pan_handles_none():
    assert _mask_pan(None) is None
    assert _mask_pan("") is None
