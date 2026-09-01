"""D2 — pinned fixture set for the disclosure narration groundedness judge.

docs/specs/disclosure-narration-judge.md, Minimum Build Slice #2. Each fixture is the
subset of `DisclosureState` that `DisclosureCoordinator._narrate` actually reads and passes
to the `disclosure_narrate` prompt: `application_id`, `term_months`, `note_rate_pct` (the
state's `annual_rate`), and `checks_passed`. D3's offline judge runs each fixture through
the real prompt and grades the completion on the axes the spec defines: it states no figure
beyond `term_months`/`note_rate_pct` (a second pass over what D1's runtime guard checks
deterministically), and `officer_action` matches the spec's term cutoff.

The reference above is to the symbol, not a line: `_narrate` moved from line 413 to 561
between this fixture set's original base and `main` when D1 landed.

No real `application_id` or applicant data — `application_id` values are all >= 990001,
well clear of `db/init/002_seed.sql`'s demo rows, so a fixture can never be mistaken for a
seeded application.

`term_months` bounds (12-60 inclusive) and `note_rate_pct` bounds (>0, <=35) mirror the
Pydantic `Field` constraints enforced identically in `services/origination-service/app/
schemas.py:55` (term only), `services/disclosure-service/app/schemas.py:18` and `:123`
(both). Those are the only numeric bounds that exist anywhere in code: there is NO coded
threshold for "top of the band" or "unusually long term" anywhere in
`disclosure_narrate.py` or its callers — the system prompt (`disclosure_narrate.py`, the
`hold_for_compliance` criteria) hands the model the bare numbers with no comparison bounds.

`expected_officer_action` therefore follows the spec, not a reading of the prompt:

- **Term.** The spec defines the only cutoff that exists: `term_months > 60` routes
  `hold_for_compliance` (docs/specs/disclosure-narration-judge.md, D2). Every schema-legal
  term is <= 60, so every fixture here — including the 60- and 54-month ones — expects
  `review_and_send`. That the cutoff is unreachable in-schema is a property of the spec's
  own cutoff, recorded for D3/D4 rather than worked around here.
- **Rate: not graded.** `policies/fee_schedule.md` documents a band (7.99%–24.99% APR by
  risk band), but no code enforces it: `POLICY_RATE_PCT = 7.99`
  (`services/origination-service/app/routers/offers.py`) is a single constant applied to
  every offer, so no fixture can sit at the top of a band the code never produces. The spec
  rules this axis not gradeable and D3 does not grade it. The rate-driven fixtures below
  therefore carry `expected_officer_action: None` — they remain real inputs for the
  groundedness axis, but pinning either verdict on them would grade the criterion the spec
  just withdrew. If the band is later code-enforced, these fixtures and their expectations
  need review (spec's own Fixture staleness risk).

`expected_officer_action` is also `None` on the two `checks_passed`-mismatch fixtures: the
system prompt surfaces `checks_passed` to the model only as "Verification: PASSED (N
deterministic checks)" (disclosure_narrate.py USER_TEMPLATE) — it is never framed as a
decision input for `officer_action`, and a mismatched count cannot reach this stage in a
real run (stage 4a would already have blocked it, see `_verify`). These two fixtures exist
only to exercise the groundedness axis defensively, per the spec's D2 description; they
carry no `officer_action` expectation for D3 to grade.
"""

from __future__ import annotations

from typing import TypedDict


class NarrationFixture(TypedDict):
    id: str
    description: str
    application_id: int
    term_months: int
    note_rate_pct: float
    checks_passed: int
    expected_officer_action: str | None


NARRATION_FIXTURES: tuple[NarrationFixture, ...] = (
    {
        "id": "normal_short_term",
        "description": "Common short term at the platform's actual flat rate.",
        "application_id": 990001,
        "term_months": 24,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "normal_mid_term",
        "description": "Common mid-length term at the platform's actual flat rate.",
        "application_id": 990002,
        "term_months": 36,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "normal_platform_default_term",
        "description": (
            "48 months is disclosure-service's own schema default "
            "(OfferIn/DisclosureIn) at the flat rate every offer is actually priced at."
        ),
        "application_id": 990003,
        "term_months": 48,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "minimum_term",
        "description": "Shortest term the schema allows (Field ge=12).",
        "application_id": 990004,
        "term_months": 12,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "minimum_term_lower_rate",
        "description": "Shortest term paired with a below-flat rate; still unremarkable.",
        "application_id": 990005,
        "term_months": 12,
        "note_rate_pct": 4.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "unusually_long_term_max",
        "description": (
            "Longest term the schema allows (Field le=60) — still at or under the spec's "
            "`term_months > 60` cutoff, so it routes review_and_send."
        ),
        "application_id": 990006,
        "term_months": 60,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "unusually_long_term_near_max",
        "description": "Just under the schema's term ceiling; same cutoff reasoning.",
        "application_id": 990007,
        "term_months": 54,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "review_and_send",
    },
    {
        "id": "rate_at_schema_ceiling",
        "description": (
            "Exactly the schema's rate ceiling (Field le=35). Rate axis is not graded — "
            "the documented band is code-unenforced, so no fixture can be the top of it."
        ),
        "application_id": 990008,
        "term_months": 48,
        "note_rate_pct": 35.0,
        "checks_passed": 5,
        "expected_officer_action": None,
    },
    {
        "id": "rate_near_schema_ceiling",
        "description": "Just under the schema's rate ceiling; rate axis not graded.",
        "application_id": 990009,
        "term_months": 48,
        "note_rate_pct": 29.99,
        "checks_passed": 5,
        "expected_officer_action": None,
    },
    {
        "id": "rate_documented_upper_end",
        "description": (
            "24.99% is policies/fee_schedule.md's stated (code-unenforced) upper end of "
            "the standard note-rate range; every issued offer is actually priced at 7.99%, "
            "so this rate has never occurred in practice. Rate axis not graded."
        ),
        "application_id": 990010,
        "term_months": 36,
        "note_rate_pct": 24.99,
        "checks_passed": 5,
        "expected_officer_action": None,
    },
    {
        "id": "rate_at_ceiling_with_max_term",
        "description": (
            "Both schema ceilings at once. Term is within the spec's cutoff, but the rate "
            "makes the expected action ungradeable, so no verdict is pinned."
        ),
        "application_id": 990011,
        "term_months": 60,
        "note_rate_pct": 35.0,
        "checks_passed": 5,
        "expected_officer_action": None,
    },
    {
        "id": "checks_passed_below_expected",
        "description": (
            "checks_passed below len(FIGURE_FIELDS) (5) — unreachable in a real run "
            "(stage 4a blocks first); exercises the groundedness axis defensively only."
        ),
        "application_id": 990012,
        "term_months": 36,
        "note_rate_pct": 7.99,
        "checks_passed": 3,
        "expected_officer_action": None,
    },
    {
        "id": "checks_passed_zero",
        "description": "checks_passed at zero — same defensive purpose as the fixture above.",
        "application_id": 990013,
        "term_months": 36,
        "note_rate_pct": 7.99,
        "checks_passed": 0,
        "expected_officer_action": None,
    },
)
