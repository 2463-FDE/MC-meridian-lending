# Client asks — decisioning governance phase

**Audience:** Dana (VP Lending Ops, Meridian) · **Written:** 2026-08-12

| # | Ask | Dana's response |
|---|-----|---|
| 1 | Reason-truncation defect: decisioning computes and stores up to 4 principal reasons (Reg B allows up to 4), but the applicant/officer screens show only the first — origination reads the legacy single-reason field instead of the full list. Small fix either way; when do you want it to land? | — |
| 2 | Adverse-action notice owner: platform shows a reason on screen but sends no letter/email, no delivery record, nothing tracking the 30-day clock. Assumption: notices go out from a separate system (letter service / servicing vendor) — which one, and does it get all 4 reasons or the truncated one? If the screen actually is the notice, that's a compliance gap needing an owner this week. | — |
| 3 | **The core question — decides the monitoring design, not a setting inside it:** does Meridian hold applicant race/ethnicity/sex/marital-status data anywhere (CRM, paper, servicing vendor), and is any product dwelling-secured? With no such data + unsecured book, Reg B generally bars collecting it, so monitoring must infer group membership from surname/geography — joining names/addresses back to decision records deliberately built to hold neither. Default without an answer: no such data, all unsecured, design written on that assumption and marked as one. | — |
| 4 | Self-decision authz gap: no check compares the caller's identity to the applicant's on the decisioning route — a staff account can trigger/read a decision on its own application. Proposed fix is scoped to `run_decision` only (two broader options considered and rejected). Add this cycle, or card for later? | — |
| 5 | (No answer needed, FYI) If a licensed vendor model is replacing the deterministic scorecard stand-in, its name/version is worth naming now — the model card is written against whatever is actually deciding. | — |
