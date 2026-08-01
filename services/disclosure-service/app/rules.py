"""Fee schedule + TILA tolerance, loaded from policy config. Fails closed.

The origination fee used to live as three hardcoded constants (apr.py 0.025, fees.py
0.030, offer.py 0.03) against a published 3.0%, so a single disclosure could carry two
different rates. This module is the one source: `policies/fee_schedule.json`, versioned,
and every disclosure records the version it was computed under.

FAIL CLOSED. A missing, malformed, or out-of-range schedule raises — there is no default
rate. A disclosure computed from a silently defaulted fee is the defect class this module
exists to remove, and it would be indistinguishable from a correct one after the fact.
`/health` surfaces the error so the service reports unhealthy instead of issuing
disclosures it cannot justify.

Path resolution: FEE_SCHEDULE_PATH wins if set, then the container mount
(/app/policies, provided by docker-compose), then the repo checkout (for tests and local
runs). The Dockerfile deliberately does not COPY policies/ — an image run without the
mount fails the loader rather than falling back to a compiled-in rate.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

_CONTAINER_PATH = Path("/app/policies/fee_schedule.json")
_REPO_PATH = Path(__file__).resolve().parents[3] / "policies" / "fee_schedule.json"


class RulesConfigError(RuntimeError):
    """Policy config is missing, unparseable, or out of range."""


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    origination_fee_pct: float
    late_fee_flat: float
    nsf_fee: float
    apr_tolerance_pp: float


# Bounds are sanity rails, not policy: they catch a decimal-point slip or a percent/
# fraction mix-up (3.0 written where 0.03 was meant), which is exactly how a plausible
# but wrong rate would reach a borrower.
_BOUNDS = {
    "origination_fee_pct": (0.0, 0.25),
    "late_fee_flat": (0.0, 1000.0),
    "nsf_fee": (0.0, 1000.0),
    "apr_tolerance_pp": (0.0, 1.0),
}

_cached: FeeSchedule | None = None


def _resolve_path() -> Path:
    override = os.getenv("FEE_SCHEDULE_PATH")
    if override:
        return Path(override)
    return _CONTAINER_PATH if _CONTAINER_PATH.exists() else _REPO_PATH


def load_fee_schedule(path: Path | None = None) -> FeeSchedule:
    """Read and validate the schedule. Raises RulesConfigError; never returns a default."""
    path = path or _resolve_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RulesConfigError(f"fee schedule not found at {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RulesConfigError(f"fee schedule at {path} is unreadable: {exc}") from exc

    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RulesConfigError("fee schedule has no version")

    values = {}
    for field, (low, high) in _BOUNDS.items():
        value = raw.get(field)
        # bool is an int subclass; a JSON true would otherwise validate as 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RulesConfigError(
                f"fee schedule field {field!r} is missing or not a number"
            )
        if not low <= value <= high:
            raise RulesConfigError(
                f"fee schedule field {field!r} = {value} is outside [{low}, {high}]"
            )
        values[field] = float(value)

    return FeeSchedule(version=version, **values)


def get_fee_schedule() -> FeeSchedule:
    """Process-cached schedule. Raises RulesConfigError if it cannot be loaded."""
    global _cached
    if _cached is None:
        _cached = load_fee_schedule()
    return _cached


def reset_cache() -> None:
    """Drop the cached schedule (tests, and any future config reload)."""
    global _cached
    _cached = None


def config_error() -> str | None:
    """None when the schedule loads, else the reason — surfaced by /health."""
    try:
        get_fee_schedule()
        return None
    except RulesConfigError as exc:
        return str(exc)
