"""D2 — pinned fixture set for the disclosure narration groundedness judge.

docs/specs/disclosure-narration-judge.md, Minimum Build Slice #2. Each fixture is the
subset of `DisclosureState` that `_narrate` (disclosure_coordinator.py:497) actually reads
and passes to the `disclosure_narrate` prompt: `application_id`, `term_months`,
`note_rate_pct` (the state's `annual_rate`), and `checks_passed`. D3's offline judge runs
each fixture through the real prompt and grades the completion on two axes: it states no
figure beyond `term_months`/`note_rate_pct` (a second pass over what D1's runtime guard
checks deterministically), and `officer_action` matches what the system prompt's own
criteria would pick.

No real `application_id` or applicant data — `application_id` values are all >= 990001,
well clear of `db/init/002_seed.sql`'s demo rows, so a fixture can never be mistaken for a
seeded application.

`term_months` bounds (12-60 inclusive) and `note_rate_pct` bounds (>0, <=35) mirror the
Pydantic `Field` constraints enforced identically in `services/origination-service/app/
schemas.py:55`, `services/disclosure-service/app/schemas.py:18` and `:123`. Those are the
only numeric bounds that exist anywhere in code: there is NO coded threshold for "top of
the band" or "unusually long term" anywhere in `disclosure_narrate.py` or its callers —
the system prompt hands the model the bare numbers with no comparison bounds, so what
counts as either phrase is a judgment call the model makes, not a validated rule. The
`expected_officer_action` on the "top_of_band"/"unusually_long_term" fixtures below is this
spec's own reasoned reading of the system prompt's stated criteria, not a code-enforced
fact — if a future PR adds a real coded band, these fixtures and their expectations need
review (spec's own Fixture staleness risk).

`expected_officer_action` is `None` on the two `checks_passed`-mismatch fixtures: the
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
        "description": "Longest term the schema allows (Field le=60).",
        "application_id": 990006,
        "term_months": 60,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "hold_for_compliance",
    },
    {
        "id": "unusually_long_term_near_max",
        "description": "Just under the schema's term ceiling.",
        "application_id": 990007,
        "term_months": 54,
        "note_rate_pct": 7.99,
        "checks_passed": 5,
        "expected_officer_action": "hold_for_compliance",
    },
    {
        "id": "top_of_band_rate_code_ceiling",
        "description": "Exactly the schema's rate ceiling (Field le=35).",
        "application_id": 990008,
        "term_months": 48,
        "note_rate_pct": 35.0,
        "checks_passed": 5,
        "expected_officer_action": "hold_for_compliance",
    },
    {
        "id": "top_of_band_rate_near_ceiling",
        "description": "Just under the schema's rate ceiling.",
        "application_id": 990009,
        "term_months": 48,
        "note_rate_pct": 29.99,
        "checks_passed": 5,
        "expected_officer_action": "hold_for_compliance",
    },
    {
        "id": "top_of_band_rate_documented_threshold",
        "description": (
            "24.99% is policies/fee_schedule.md's stated (code-unenforced) upper end of "
            "the standard note-rate range; every issued offer is actually priced at 7.99%, "
            "so this rate has never occurred in practice."
        ),
        "application_id": 990010,
        "term_months": 36,
        "note_rate_pct": 24.99,
        "checks_passed": 5,
        "expected_officer_action": "hold_for_compliance",
    },
    {
        "id": "combined_extreme_term_and_rate",
        "description": "Both the term and rate ceilings at once.",
        "application_id": 990011,
        "term_months": 60,
        "note_rate_pct": 35.0,
        "checks_passed": 5,
        "expected_officer_action": "hold_for_compliance",
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
