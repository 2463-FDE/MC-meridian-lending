-- Meridian Lending — seed data

-- Staff + one borrower login. password_hash is sha256('password') for every seeded user
-- (Halcyon shipped them all with the same demo password and never forced a reset).
INSERT INTO users (username, password_hash, role, display_name, applicant_id) VALUES
  ('admin',       '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'admin',       'Dana Whitfield (VP Lending Ops)', NULL),
  ('underwriter', '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'underwriter', 'Sam Okafor (Underwriting)',       NULL),
  ('csr',         '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'csr',         'Jordan Reyes (Servicing Rep)',    NULL),
  ('maria',       '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8', 'borrower',    'Maria Gonzalez',                  1);

INSERT INTO applicants (id, name, dob, ssn, ein, is_entity, email, phone, address) VALUES
  (1, 'Maria Gonzalez', '1971-03-02', '412-55-9981', NULL, FALSE, 'maria.gonzalez@example.com', '559-555-0118', '118 Larkspur Ave, Fresno, CA 93722'),
  (2, 'Darnell Webb',   '1985-12-09', '501-22-7733', NULL, FALSE, 'd.webb@example.com',         '419-555-0009', '9 Cedar Ct, Toledo, OH 43604'),
  (3, 'Priya Raman',    '1989-07-14', '622-41-0098', NULL, FALSE, 'priya.raman@example.com',    '512-555-0740', '740 Birch St, Austin, TX 78702'),
  (4, 'Travis Booker',  '1992-04-21', '330-90-5512', NULL, FALSE, 'tbooker@example.com',        '901-555-0055', '55 Plum Rd, Memphis, TN 38106'),
  (5, 'Aisha Bello',    '1990-10-30', '447-08-2261', NULL, FALSE, 'aisha.bello@example.com',    '901-555-0012', '12 Quince Way, Memphis, TN 38114'),
  (6, 'Northgate Holdings LLC', NULL, NULL, '47-2210098', TRUE, 'ap@northgateholdings.example', '302-555-0200', '200 Commerce Plaza, Wilmington, DE 19801');
SELECT setval('applicants_id_seq', 6);

INSERT INTO applications (id, applicant_id, amount, term_months, purpose, income, employer, job_title, employment_years, status) VALUES
  (4471, 1, 18000, 48, 'debt_consolidation', 52000, 'Valley Health System', 'RN',               9,  'funded'),
  (5582, 2, 12000, 36, 'auto',               47000, 'Toledo Logistics Co',  'Dispatcher',       4,  'funded'),
  (6011, 3, 15000, 36, 'home_improvement',   84000, 'Lone Star Software',   'Engineer',         6,  'funded'),
  (6012, 4,  9000, 24, 'personal',           31000, 'Bluff City Retail',    'Shift Lead',       2,  'decided'),
  (6013, 5,  8000, 24, 'personal',           29500, 'Memphis Care Partners','CNA',              1,  'decided'),
  (6014, 6, 50000, 60, 'working_capital',   240000, NULL,                   NULL,               NULL,'funded');
SELECT setval('applications_id_seq', 6014);

-- KYC: CIP fields only; the entity (6/6014) cleared with no UBO and no sanctions screen.
INSERT INTO kyc_checks (applicant_id, name_verified, dob_verified, address_verified, ssn_verified) VALUES
  (1, TRUE, TRUE, TRUE, TRUE),
  (2, TRUE, TRUE, TRUE, TRUE),
  (3, TRUE, TRUE, TRUE, TRUE),
  (4, TRUE, TRUE, TRUE, TRUE),
  (5, TRUE, TRUE, TRUE, TRUE),
  (6, TRUE, FALSE, TRUE, FALSE);   -- entity: no real person verified, cleared anyway

-- Decisions: outcome only. Denials 6012/6013 have no recorded reason anywhere.
INSERT INTO decisions (app_id, outcome) VALUES
  (4471, 'approve'),
  (5582, 'approve'),
  (6011, 'approve'),
  (6012, 'deny'),
  (6013, 'deny'),
  (6014, 'approve');

INSERT INTO offers (app_id, apr, finance_charge, monthly_payment, amount_financed, total_of_payments) VALUES
  (4471, 7.142, 3021.44, 437.94, 18000, 21021.44),  -- disclosed APR is the float value
  (5582, 9.990, 1934.10, 386.00, 12000, 13896.00),
  (6011, 7.142, 1772.55, 466.46, 15000, 16793.16),
  (6014, 11.250, 16480.00, 1093.00, 50000, 65580.00);

INSERT INTO loans (id, app_id, applicant_name, principal, apr, term_months, status) VALUES
  (4471, 4471, 'Maria Gonzalez', 18000, 7.142, 48, 'current'),
  (5582, 5582, 'Darnell Webb',   12000, 9.990, 36, 'current'),
  (6011, 6011, 'Priya Raman',    15000, 7.142, 36, 'current'),
  (6014, 6014, 'Northgate Holdings LLC', 50000, 11.250, 60, 'current');
SELECT setval('loans_id_seq', 6014);

-- Balances stored as a single overwritten float.
INSERT INTO balances (loan_id, balance, past_due) VALUES
  (4471, 12200.0, 0),
  (5582, 7999.0, 410.50),   -- note: 5582 was double-applied on 2026-06-01 (see payment log)
  (6011, 13135.64, 0),
  (6014, 49000.0, 0);

-- Payments: full PAN + CVV stored. 5582 has TWO rows for one retried charge (double-charge).
INSERT INTO payments (loan_id, pan, cvv, amount, method, created_at) VALUES
  (4471, '4111111111111111', '123', 250.00, 'card', '2026-06-01 09:14:11'),
  (5582, '5500005555555559', '887', 410.50, 'card', '2026-06-01 09:31:04'),
  (5582, '5500005555555559', '887', 410.50, 'card', '2026-06-01 09:31:06'),  -- duplicate
  (4471, '340000000000009',  '4021', 99.99, 'card', '2026-06-01 11:18:45'),
  (6011, NULL, NULL, 432.18, 'ach', '2026-06-02 08:00:00'),
  (4471, '4111111111111111', '123', 250.00, 'card', '2026-06-03 09:00:00'),
  (6011, NULL, NULL, 432.18, 'ach', '2026-06-03 08:00:00');

-- "audit" entries that are really app logging, not an actor->action->time control trail.
INSERT INTO audit_logs (actor, action, detail) VALUES
  ('system', 'payment', 'charge req pan=4111111111111111 amount=250.00'),
  ('rep_jordan', 'waive_fee', 'loan 5582 waived 35.00'),
  ('system', 'decision', 'app 6012 deny');
