"""Fee schedule loader — fail-closed behaviour and agreement with published policy.

The defect this replaces: three hardcoded ORIGINATION_FEE_PCT copies (apr.py 0.025,
fees.py 0.030, offer.py 0.03) against a published 3.0%, so one offer carried two rates.
Externalizing to a single file only helps if the file cannot silently disagree with the
published schedule, and if a bad file stops the service rather than defaulting — both
asserted here.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app import apr, fees, offer, rules

POLICY_DIR = Path(__file__).resolve().parents[3] / "policies"
FEE_SCHEDULE_JSON = POLICY_DIR / "fee_schedule.json"
FEE_SCHEDULE_MD = POLICY_DIR / "fee_schedule.md"


@pytest.fixture(autouse=True)
def _clear_cache():
    rules.reset_cache()
    yield
    rules.reset_cache()


def _write(tmp_path, payload):
    path = tmp_path / "fee_schedule.json"
    path.write_text(json.dumps(payload))
    return path


def _valid():
    return {
        "version": "test-1",
        "origination_fee_pct": 0.030,
        "late_fee_flat": 35.0,
        "nsf_fee": 25.0,
        "apr_tolerance_pp": 0.125,
    }


def test_loads_published_schedule():
    schedule = rules.load_fee_schedule(FEE_SCHEDULE_JSON)
    assert schedule.origination_fee_pct == 0.030
    assert schedule.apr_tolerance_pp == 0.125
    assert schedule.version


def test_published_json_matches_published_markdown():
    """The markdown is the human-facing schedule; the JSON is what the code reads.

    Two files holding one number is how the three-constant drift started. This asserts
    they agree, so a policy change to one that misses the other fails the build.
    """
    md = FEE_SCHEDULE_MD.read_text()
    fee_row = re.search(r"\|\s*Origination fee\s*\|\s*([\d.]+)%", md)
    assert fee_row, "origination fee row not found in fee_schedule.md"
    assert (
        float(fee_row.group(1)) / 100
        == rules.load_fee_schedule(FEE_SCHEDULE_JSON).origination_fee_pct
    )


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(rules.RulesConfigError, match="not found"):
        rules.load_fee_schedule(tmp_path / "absent.json")


def test_malformed_json_fails_closed(tmp_path):
    path = tmp_path / "fee_schedule.json"
    path.write_text("{not json")
    with pytest.raises(rules.RulesConfigError, match="unreadable"):
        rules.load_fee_schedule(path)


def test_missing_version_fails_closed(tmp_path):
    payload = _valid()
    del payload["version"]
    with pytest.raises(rules.RulesConfigError, match="version"):
        rules.load_fee_schedule(_write(tmp_path, payload))


@pytest.mark.parametrize("field", sorted(rules._BOUNDS))
def test_missing_numeric_field_fails_closed(tmp_path, field):
    payload = _valid()
    del payload[field]
    with pytest.raises(rules.RulesConfigError, match=field):
        rules.load_fee_schedule(_write(tmp_path, payload))


def test_percent_fraction_mixup_fails_closed(tmp_path):
    """3.0 written where 0.03 was meant — a 100x fee that would otherwise look plausible."""
    payload = _valid() | {"origination_fee_pct": 3.0}
    with pytest.raises(rules.RulesConfigError, match="outside"):
        rules.load_fee_schedule(_write(tmp_path, payload))


def test_boolean_is_not_a_number(tmp_path):
    payload = _valid() | {"origination_fee_pct": True}
    with pytest.raises(rules.RulesConfigError, match="not a number"):
        rules.load_fee_schedule(_write(tmp_path, payload))


def test_no_hardcoded_fee_constants_remain():
    """The three copies are deleted, not corrected — a corrected copy drifts again."""
    for module in (apr, fees, offer):
        assert not hasattr(module, "ORIGINATION_FEE_PCT"), (
            f"{module.__name__} still defines its own fee constant"
        )


def test_one_rate_reaches_every_figure_in_the_offer():
    """The self-contradiction this fixed: amount_financed on a $540 fee (3.0%) sitting
    beside an APR computed from a $450 fee (2.5%), inside one returned dict.

    Asserted against the schedule rather than against a formula, so it keeps holding when
    the compute method changes (it since has — add-on gave way to actuarial).
    """
    principal = 18000.0
    fee_pct = rules.load_fee_schedule(FEE_SCHEDULE_JSON).origination_fee_pct
    expected_fee = Decimal(str(principal)) * Decimal(str(fee_pct))

    built = offer.build_offer(principal, 7.99, 48)
    assert apr.prepaid_finance_charge(principal) == expected_fee.quantize(
        Decimal("0.01")
    )
    assert built["amount_financed"] == float(
        (Decimal(str(principal)) - expected_fee).quantize(Decimal("0.01"))
    )
    # The same fee that reduced amount_financed must also sit inside the finance charge...
    assert built["finance_charge"] == float(
        (apr.interest_charge(principal, 7.99, 48) + expected_fee).quantize(
            Decimal("0.01")
        )
    )
    # ...and inside the APR: a fee-bearing loan prices above its note rate.
    assert built["apr"] > 7.99


def test_changing_the_rate_moves_every_figure_together(monkeypatch):
    """Guards the coupling itself: one source means one rate change reaches all of them.

    Under the old three-constant layout, editing one copy moved one figure and left the
    others on their own rate — which is precisely the state this replaced.
    """
    principal = 18000.0
    base = offer.build_offer(principal, 7.99, 48)

    schedule = rules.load_fee_schedule(FEE_SCHEDULE_JSON)
    doubled = type(schedule)(**{**vars(schedule), "origination_fee_pct": 0.060})
    monkeypatch.setattr(rules, "get_fee_schedule", lambda: doubled)
    raised = offer.build_offer(principal, 7.99, 48)

    assert raised["amount_financed"] < base["amount_financed"]
    assert raised["finance_charge"] > base["finance_charge"]
    assert raised["apr"] > base["apr"]


def test_health_reports_unhealthy_when_schedule_unloadable(monkeypatch, tmp_path):
    """An otherwise-healthy service with an unloadable schedule must not read ready."""
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app as fastapi_app

    # Satisfy the secret and DB rungs so the rules rung is what fails, not a stub.
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:the_real_pw@postgres:5432/meridian",
    )
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "internal-token")
    monkeypatch.setattr(config, "database_reachable", lambda *a, **k: (True, None))

    monkeypatch.setenv("FEE_SCHEDULE_PATH", str(tmp_path / "absent.json"))
    rules.reset_cache()
    response = TestClient(fastapi_app).get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert "rules_config_error" in body


def test_health_ok_when_schedule_loads(monkeypatch):
    """Guard against the previous test passing for the wrong reason."""
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app as fastapi_app

    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:the_real_pw@postgres:5432/meridian",
    )
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "internal-token")
    monkeypatch.setattr(config, "database_reachable", lambda *a, **k: (True, None))

    monkeypatch.setenv("FEE_SCHEDULE_PATH", str(FEE_SCHEDULE_JSON))
    rules.reset_cache()
    response = TestClient(fastapi_app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
