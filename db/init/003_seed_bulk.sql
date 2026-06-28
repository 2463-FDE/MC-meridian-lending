-- Meridian Lending — bulk seed (synthetic portfolio for a realistic dashboard).
--
-- Generates ~300 applicants, ~300 applications, 300 decisions, ~180 funded loans with
-- balances + offers, and ~600 payments. Money is DOUBLE PRECISION throughout (same float
-- debt as the rest of the platform). IDs start at 100 (applicants) / 7000 (apps+loans) so
-- they never collide with the hand-curated anchor rows in 002_seed.sql (1..6 / 4471..6014).

-- 300 borrowers (ids 100..399)
INSERT INTO applicants (id, name, dob, ssn, email, phone, is_entity, address)
SELECT g,
  (ARRAY['James','Mary','Robert','Patricia','John','Jennifer','Michael','Linda','David','Elizabeth','William','Barbara','Richard','Susan','Joseph','Jessica','Thomas','Karen','Charles','Nancy'])[1 + (g % 20)]
    || ' ' ||
  (ARRAY['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez','Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin'])[1 + ((g * 7) % 20)],
  (DATE '1960-01-01' + ((g * 97) % 14000)),
  lpad(((g * 131) % 900 + 100)::text, 3, '0') || '-' || lpad(((g * 17) % 90 + 10)::text, 2, '0') || '-' || lpad(((g * 53) % 9000 + 1000)::text, 4, '0'),
  'user' || g || '@example.com',
  '555-01' || lpad((g % 100)::text, 2, '0'),
  FALSE,
  ((g * 13) % 900 + 100)::text || ' ' || (ARRAY['Oak','Maple','Cedar','Pine','Elm','Birch','Walnut','Spruce'])[1 + (g % 8)] || ' St, ' ||
    (ARRAY['Fresno, CA 93722','Toledo, OH 43604','Austin, TX 78702','Memphis, TN 38106','Akron, OH 44303','Mesa, AZ 85201','Tulsa, OK 74103','Omaha, NE 68102'])[1 + ((g * 3) % 8)]
FROM generate_series(100, 399) g;
SELECT setval('applicants_id_seq', 399);

-- 300 applications (ids 7000..7299), applicant_id = 100 + (id - 7000)
INSERT INTO applications (id, applicant_id, amount, term_months, purpose, income, employer, job_title, employment_years, status)
SELECT g,
  100 + (g - 7000),
  (1000 + ((g * 263) % 49000))::double precision,
  (ARRAY[12,24,36,48,60])[1 + ((g * 3) % 5)],
  (ARRAY['debt_consolidation','home_improvement','auto','medical','personal','other'])[1 + ((g * 7) % 6)],
  (24000 + ((g * 311) % 180000))::double precision,
  (ARRAY['Acme Corp','Globex','Initech','Umbrella Co','Hooli','Stark Industries','Wayne Enterprises','Soylent Inc'])[1 + ((g * 5) % 8)],
  (ARRAY['Analyst','Manager','Technician','Clerk','Engineer','Driver','Nurse','Teacher'])[1 + ((g * 11) % 8)],
  ((g % 15) + 1)::double precision,
  (ARRAY['funded','funded','funded','decided','submitted'])[1 + ((g * 2) % 5)]
FROM generate_series(7000, 7299) g;
SELECT setval('applications_id_seq', 7299);

-- A decision row for every application (outcome only — the audit-trail debt is preserved).
INSERT INTO decisions (app_id, outcome)
SELECT g, (ARRAY['approve','approve','approve','deny','refer'])[1 + ((g * 2) % 5)]
FROM generate_series(7000, 7299) g;

-- Loans for every FUNDED application (loan id = app id, mirroring the anchor convention).
INSERT INTO loans (id, app_id, applicant_name, principal, apr, term_months, status)
SELECT a.id, a.id, ap.name, a.amount,
  round((7.99 + (a.id % 16))::numeric, 3)::double precision,
  a.term_months,
  (ARRAY['current','current','current','delinquent','paid_off'])[1 + ((a.id * 5) % 5)]
FROM applications a JOIN applicants ap ON ap.id = a.applicant_id
WHERE a.id BETWEEN 7000 AND 7299 AND a.status = 'funded';
SELECT setval('loans_id_seq', 7299);

-- Offers for those funded loans (float APR / finance charge — TILA rounding debt preserved).
INSERT INTO offers (app_id, apr, finance_charge, monthly_payment, amount_financed, total_of_payments)
SELECT l.app_id, l.apr,
  round((l.principal * 0.16)::numeric, 2)::double precision,
  round((l.principal / l.term_months * 1.1)::numeric, 2)::double precision,
  round((l.principal * 0.97)::numeric, 2)::double precision,
  round((l.principal * 1.16)::numeric, 2)::double precision
FROM loans l WHERE l.id BETWEEN 7000 AND 7299;

-- Balances: single mutable float column (no ledger — debt preserved).
INSERT INTO balances (loan_id, balance, past_due)
SELECT l.id,
  round((l.principal * (0.30 + ((l.id % 60) / 100.0)))::numeric, 2)::double precision,
  CASE WHEN l.status = 'delinquent' THEN round((50 + (l.id % 400))::numeric, 2)::double precision ELSE 0 END
FROM loans l WHERE l.id BETWEEN 7000 AND 7299;

-- 1..6 payments per loan (~600 rows). Card payments store the FULL PAN + CVV (debt preserved).
INSERT INTO payments (loan_id, pan, cvv, amount, method, created_at)
SELECT l.id,
  CASE WHEN (l.id + s) % 3 = 0 THEN NULL ELSE '4111111111111111' END,
  CASE WHEN (l.id + s) % 3 = 0 THEN NULL ELSE '123' END,
  round((l.principal / l.term_months)::numeric, 2)::double precision,
  CASE WHEN (l.id + s) % 3 = 0 THEN 'ach' ELSE 'card' END,
  TIMESTAMPTZ '2026-05-01 09:00:00' + ((l.id % 20) || ' days')::interval + (s || ' days')::interval
FROM loans l CROSS JOIN LATERAL generate_series(1, 1 + (l.id % 5)) AS s
WHERE l.id BETWEEN 7000 AND 7299;
