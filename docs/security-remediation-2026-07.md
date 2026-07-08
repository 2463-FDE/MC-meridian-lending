# Security Remediation — Committed Secrets & PII Logs (2026-07)

**Severity:** P0 / release-blocking
**Source:** PR review (LLM client feature), findings F1 + F2
**Debt refs:** D1 (hardcoded/committed creds), D5 (plaintext PAN/CVV/SSN in logs), D13 (PAN/CVV)

## What was wrong

The repository tracked live-looking secrets and raw cardholder/PII data:

- **`.env`** (tracked): `POSTGRES_PASSWORD`, `DB_PASSWORD`, `CORE_BANKING_API_KEY`,
  `EXPERIAN_KEY`, `PROCESSOR_API_KEY`.
- **Log files** (tracked, `.gitignore` explicitly kept them): raw PAN, CVV, full
  SSN, names, and a leaked `EXPERIAN_KEY` in clear text —
  `logs/payment-service.log`, `services/servicing-service/logs/payment-service.log`,
  `services/origination-service/logs/origination-service.log`.
- **Hardcoded secrets in source** (the same credentials, duplicated inline so the
  demo "just works"): `EXPERIAN_KEY = "EXAMPLE-LEAKED-KEY-rotate-me"` and
  `CORE_BANKING_API_KEY`/`PROCESSOR_API_KEY` `os.getenv` fallbacks (`cb_live_…`,
  `proc_live_…`) across the service `config.py` files, plus the Postgres password
  `meridian_dev_pw_2024` in all seven `config.py` `DATABASE_URL` defaults and in
  `docker-compose.yml`. Untracking `.env` alone would NOT have removed these — they
  ship in every image and stay in history.

These predate the LLM-client feature (committed in `a726640`) and exist on `main`.

## What this branch does

- `git rm --cached` on `.env` and the three tracked log files (local copies kept).
- `.gitignore`: ignore `.env` / `.env.local` / `*.pem` / `*.key` / `*.log`; keep
  `.env.example`. Removed the "keep logs checked in" rule.
- `.env.example` already tracked (all required keys, placeholder values) — no
  populated `.env` is needed to set up locally; its header no longer claims a
  committed `.env` exists.
- **Removed the hardcoded secrets from source**: `EXPERIAN_KEY`,
  `CORE_BANKING_API_KEY`, and `PROCESSOR_API_KEY` are now `os.getenv(name, "")`
  (env only, no secret default) across all service `config.py`; the
  `DATABASE_URL` fallbacks drop the inline password; `docker-compose.yml` now
  requires `POSTGRES_PASSWORD` (`:?` — fails fast with a message instead of
  falling back to a committed default). Missing bureau/processor keys now fail
  closed at call time rather than silently using a leaked key.

## What this branch does NOT do — MUST still happen

Each item has an explicit owner and target date so nothing stalls. Dates are
proposed (P0) — confirm at the next standup; `_TBD_` owners must be named there.

1. **Rotate every exposed credential** — they remain valid until rotated, and
   `git rm --cached` does **not** remove them from git history (any past commit
   still contains them). Untracking stops *future* distribution only.
   *Owner: Sam (Controller) — accountable; each key rotated by its system owner.*
   *Due: 2026-07-10.*
   - [ ] Postgres password (`POSTGRES_PASSWORD` / `DB_PASSWORD`) — _owner: TBD (platform/DB)_
   - [ ] `CORE_BANKING_API_KEY` — _owner: TBD (core-banking integration)_
   - [ ] `EXPERIAN_KEY` — _owner: Priya (Compliance/BSA)_
   - [ ] `PROCESSOR_API_KEY` — _owner: Sam (Controller)_
2. **Scrub git history** — after rotation, purge the blobs from history with
   `git filter-repo` (or BFG) and force-push, coordinated across all open
   branches (`feature/pii-redaction`, `feature/llm-client-week1`,
   `feature/rag-eval-week2`). This is history-rewriting — schedule with the team.
   *Owner: _TBD_ (eng lead) — name at standup. Due: 2026-07-15 (after rotation).*
3. **Provision runtime secrets** — inject via host env / secret manager (the LLM
   feature already does this for `CLAUDE_API_KEY`); do not recreate a committed
   `.env`.
   *Owner: _TBD_ (eng lead). Due: 2026-07-10.*

Overall remediation owners (per README): Priya (Compliance/BSA), Dana (VP Lending
Ops), Sam (Controller).

## Verification

Run from the repo root; each must produce the expected output before the
untracking part of this remediation is considered done:

- No `.env` or log file is tracked:
  ```
  git ls-files | grep -E '(^\.env$|/\.env$|\.log$)'
  ```
  Expected: **no output** (exit 1).
- `.env.example` (the safe template) is still tracked:
  ```
  git ls-files | grep -E '(^|/)\.env\.example$'
  ```
  Expected: `.env.example`.
- No hardcoded secret literals remain in source/compose:
  ```
  git grep -nIE 'cb_live_|proc_live_|EXAMPLE-LEAKED-KEY|meridian_dev_pw_2024' -- 'services/*' docker-compose.yml
  ```
  Expected: **no output**.
- No secrets remain in the working tree (adjust tool to your scanner):
  ```
  gitleaks detect --no-banner --redact
  ```
  Expected: `no leaks found`.

Note: a clean scan of the **working tree** does not clear git **history** — items
1 and 2 above (rotate + scrub) remain the real release blockers.
