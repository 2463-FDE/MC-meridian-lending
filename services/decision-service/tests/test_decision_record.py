"""Decision-record write-path tests (ADR 0009 §4, ADR 0008 req. 2, spec D3).

decide() must persist an append-only decision_events row atomically with the outcome —
or refuse the decision. These tests stub app.decision.db.query to capture what would be
written (no live Postgres in unit tests; the DB trigger and end-to-end write are covered
by the smoke test against the compose stack).
"""

import json

import pytest

from app import config, decision, model_vendor


@pytest.fixture
def synthetic_mode(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")


@pytest.fixture
def captured_events(monkeypatch):
    """Capture the atomic event-write statement's params instead of hitting Postgres."""
    captured = []

    def _capture(sql, params=None):
        assert "INSERT INTO decision_events" in sql
        assert "INSERT INTO decisions" in sql  # one atomic statement, not two calls
        captured.append(params)
        return []

    monkeypatch.setattr(decision.db, "query", _capture)
    return captured


STRONG_APP = {
    "app_id": 11,
    "ssn": "123456782",
    "income": 100000,
    "amount": 15000,
    "term_months": 36,
    "monthly_debt": 0,
    "employment_years": 5,
}
WEAK_APP = {
    "app_id": 12,
    "ssn": "123456781",
    "income": 0,
    "amount": 15000,
    "term_months": 36,
    "monthly_debt": 0,
    "employment_years": 0,
}


def test_approve_persists_event_with_drivers_and_empty_reasons(
    synthetic_mode, captured_events
):
    result = decision.decide(STRONG_APP)
    assert result["decision"] == "approve"
    (params,) = captured_events
    app_id, outcome, reasons_json, drivers_json, band, inputs_json, decided_by = params
    assert (app_id, outcome, band) == (11, "approve", "approve")
    assert json.loads(reasons_json) == []
    drivers = json.loads(drivers_json)
    assert drivers["model_id"] == "meridian-risk-stub"
    assert drivers["attributions"]  # ranked attributions recorded
    assert decided_by == model_vendor.model_signature()


def test_deny_persists_specific_reasons_from_top_attributions(
    synthetic_mode, captured_events
):
    result = decision.decide(WEAK_APP)
    assert result["decision"] == "deny"
    (params,) = captured_events
    reasons = json.loads(params[2])
    assert reasons, "adverse action must carry principal reasons"
    assert reasons[0]["code"] == "R02"  # zero income: payment burden is the top driver
    texts = " ".join(r["reason"].lower() for r in reasons)
    assert "purchasing history" not in texts


def test_persisted_inputs_are_identifier_free(synthetic_mode, captured_events):
    decision.decide(WEAK_APP)
    (params,) = captured_events
    inputs = json.loads(params[5])
    assert "ssn" not in inputs and "name" not in inputs
    assert inputs["bureau_score"] == 612  # model inputs are recorded


def test_persist_failure_refuses_the_decision(synthetic_mode, monkeypatch):
    def _db_down(sql, params=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(decision.db, "query", _db_down)
    with pytest.raises(decision.DecisionRecordError):
        decision.decide(STRONG_APP)


def test_adverse_outcome_without_reasons_is_refused():
    with pytest.raises(decision.DecisionRecordError):
        decision._validate_record(
            "deny", "deny", [], {"model_score": 500}, model_vendor.model_signature()
        )


def test_system_outcome_contradicting_band_is_refused():
    # The #6012 class: score in the refer band recorded as deny with no human decider.
    with pytest.raises(decision.DecisionRecordError):
        decision._validate_record(
            "deny",
            "refer",
            [{"code": "R01", "reason": "x", "feature": "delinquency_history"}],
            {"model_score": 612},
            model_vendor.model_signature(),
        )


def test_human_override_contradicting_band_is_allowed():
    decision._validate_record(
        "deny",
        "refer",
        [{"code": "R01", "reason": "x", "feature": "delinquency_history"}],
        {"model_score": 612},
        "underwriter:jane",
    )


# --- GET /decisions/{app_id}/record (memory-tool projection, ADR 0009 §5) ----------


def _record_client(monkeypatch, responses):
    """TestClient with routers.decisions.db.query returning canned rows per call."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import decisions as decisions_router

    calls = iter(responses)
    monkeypatch.setattr(
        decisions_router.db, "query", lambda sql, params=None: next(calls)
    )
    return TestClient(app)


def test_record_endpoint_returns_recorded_event(monkeypatch):
    import datetime

    event_row = {
        "outcome": "deny",
        "principal_reasons": [
            {
                "code": "R02",
                "reason": "Excessive obligations in relation to income",
                "feature": "payment_burden",
            }
        ],
        "drivers": {"model_id": "meridian-risk-stub", "model_score": 518},
        "policy_band": "deny",
        "inputs": {"bureau_score": 612},
        "decided_by": "meridian-risk-stub:v1",
        "decided_at": datetime.datetime(2026, 7, 15, 12, 0, 0),
    }
    resp = _record_client(monkeypatch, [[event_row]]).get("/decisions/12/record")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "recorded"
    assert body["principal_reasons"][0]["code"] == "R02"
    assert body["decided_at"].startswith("2026-07-15")


def test_record_endpoint_distinguishes_legacy_no_record(monkeypatch):
    # decisions row exists (pre-feature outcome) but no event: reasons unrecoverable.
    resp = _record_client(monkeypatch, [[], [{"outcome": "deny"}]]).get(
        "/decisions/6012/record"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_record_legacy"
    assert body["outcome"] == "deny"
    assert body["principal_reasons"] == []


def test_record_endpoint_404_when_never_decisioned(monkeypatch):
    resp = _record_client(monkeypatch, [[], []]).get("/decisions/999/record")
    assert resp.status_code == 404


# --- Adversarial-review fixes (teeth 2026-07-15) ------------------------------------


BORDERLINE_APP = {
    # score 647: refer band with ALL feature contributions >= 0 (teeth H1 case)
    "app_id": 13,
    "ssn": "123456782",
    "income": 25200,
    "amount": 10000,
    "term_months": 36,
    "monthly_debt": 450,
    "employment_years": 4.5,
}


def test_refer_with_no_negative_drivers_is_decisionable(
    synthetic_mode, captured_events, monkeypatch
):
    # H1: the original rule refused this applicant class forever (503 + a fresh
    # bureau pull per retry). Refer routes to manual review; empty reasons are honest.
    monkeypatch.setattr(decision, "_pull_credit", lambda ssn: 650)
    result = decision.decide(BORDERLINE_APP)
    assert result["decision"] == "refer"
    assert result["principal_reasons"] == []
    (params,) = captured_events
    assert json.loads(params[2]) == []  # recorded honestly with no reasons


def test_refer_with_negative_drivers_still_requires_reasons():
    drivers = {"attributions": [{"feature": "payment_burden", "contribution": -12.0}]}
    with pytest.raises(decision.DecisionRecordError):
        decision._validate_record(
            "refer", "refer", [], drivers, model_vendor.model_signature()
        )


def test_deny_without_reasons_still_refused_even_with_no_negative_drivers():
    with pytest.raises(decision.DecisionRecordError):
        decision._validate_record(
            "deny", "deny", [], {"attributions": []}, model_vendor.model_signature()
        )


def test_approve_path_validates_model_vocabulary(
    synthetic_mode, captured_events, monkeypatch
):
    # M2: a model whose features we cannot explain must not decide in ANY direction.
    from app import reasons as reasons_mod

    monkeypatch.setattr(reasons_mod, "REASON_MAP", {})
    with pytest.raises(reasons_mod.UnmappedFeatureError):
        decision.decide(STRONG_APP)  # approve-band applicant
    assert captured_events == []  # nothing persisted


def test_unmapped_feature_returns_typed_503(synthetic_mode, monkeypatch):
    # M1: the fail-closed vocabulary gate is a policy refusal, not a 500.
    from fastapi.testclient import TestClient

    from app import reasons as reasons_mod
    from app.main import app

    monkeypatch.setattr(reasons_mod, "REASON_MAP", {})
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/decisions",
        json={
            "application_id": 9,
            "applicant_id": 9,
            "name": "Test Applicant",
            "ssn": "123456781",
            "requested_amount": 15000,
            "term_months": 36,
            "annual_income": 0,
            "monthly_debt": 0,
        },
    )
    assert resp.status_code == 503
    assert "no mapped adverse-action reason" in resp.json()["detail"]
