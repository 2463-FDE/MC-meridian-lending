"""Content fingerprint for a persisted disclosure.

hash(inputs + ruleset + outputs). Its job is narrow and worth stating so it is not
mistaken for something stronger:

  * It DETECTS a post-hoc edit to a stored disclosure — recompute from the row and compare.
  * It does NOT authenticate. It is an unkeyed digest, so anyone who can edit the row can
    recompute it. Tamper-evidence against accident and drift, not against an attacker with
    write access; that is what the delivered-row freeze trigger is for.

Stability matters more than elegance here: the digest must reproduce byte-for-byte years
later, from the persisted snapshot alone. So the payload is canonical JSON — sorted keys,
no whitespace variance, money as integer minor units and APR as a decimal string. Never
float: 0.1 + 0.2 does not round-trip, and a fingerprint that changes with the platform's
float formatting is worse than none.
"""

import hashlib
import json
from decimal import Decimal

FINGERPRINT_VERSION = "fp-1"


def _canonical(value):
    """Decimals become strings; everything else must already be JSON-stable."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        raise TypeError(
            "float in fingerprint payload — use int minor units or Decimal; "
            "float formatting is not stable enough for a legal record"
        )
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def compute_fingerprint(
    *,
    inputs: dict,
    fee_schedule_version: str,
    apr_method_version: str,
    outputs: dict,
) -> str:
    """Return `fp-1:<sha256>` over the canonical payload.

    The version prefix is what makes the scheme changeable: a future payload shape gets a
    new prefix rather than silently producing different digests for the same disclosure.
    """
    payload = {
        "version": FINGERPRINT_VERSION,
        "inputs": _canonical(inputs),
        "ruleset": {
            "fee_schedule_version": fee_schedule_version,
            "apr_method_version": apr_method_version,
        },
        "outputs": _canonical(outputs),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(encoded).hexdigest()}"
