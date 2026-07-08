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

These predate the LLM-client feature (committed in `a726640`) and exist on `main`.

## What this branch does

- `git rm --cached` on `.env` and the three tracked log files (local copies kept).
- `.gitignore`: ignore `.env` / `.env.local` / `*.pem` / `*.key` / `*.log`; keep
  `.env.example`. Removed the "keep logs checked in" rule.

## What this branch does NOT do — MUST still happen

1. **Rotate every exposed credential** — they remain valid until rotated, and
   `git rm --cached` does **not** remove them from git history (any past commit
   still contains them). Untracking stops *future* distribution only.
   - [ ] Postgres password (`POSTGRES_PASSWORD` / `DB_PASSWORD`)
   - [ ] `CORE_BANKING_API_KEY`
   - [ ] `EXPERIAN_KEY`
   - [ ] `PROCESSOR_API_KEY`
2. **Scrub git history** — after rotation, purge the blobs from history with
   `git filter-repo` (or BFG) and force-push, coordinated across all open
   branches (`feature/pii-redaction`, `feature/llm-client-week1`,
   `feature/rag-eval-week2`). This is history-rewriting — schedule with the team.
3. **Provision runtime secrets** — inject via host env / secret manager (the LLM
   feature already does this for `CLAUDE_API_KEY`); do not recreate a committed
   `.env`.

Owners: Priya (Compliance/BSA), Dana (VP Lending Ops), Sam (Controller) per README.
