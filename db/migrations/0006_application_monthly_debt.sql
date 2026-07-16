-- PR #7 review: the LOS must send REAL model inputs to decision-service, not a
-- fabricated monthly_debt of 0. Persist the applicant's monthly debt obligations so
-- the model's debt-to-income / payment-burden driver reflects the actual application
-- instead of scoring every LOS applicant as debt-free (over-approval risk).

ALTER TABLE applications ADD COLUMN IF NOT EXISTS monthly_debt DOUBLE PRECISION;
