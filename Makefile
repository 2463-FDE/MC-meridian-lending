.PHONY: up down logs build seed ps test fmt prove

up:
	docker compose up -d --build

build:
	docker compose build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# Seed runs automatically via db/init on first `up`; this re-applies seed only.
seed:
	docker compose exec -T postgres psql -U $${POSTGRES_USER:-meridian} -d $${POSTGRES_DB:-meridian} < db/init/002_seed.sql

test:
	cd services/origination-service && python -m pytest -q || true
	cd services/servicing-service && python -m pytest -q || true
	cd services/kyc-service && python -m pytest -q || true
	cd services/decision-service && python -m pytest -q || true
	cd services/disclosure-service && python -m pytest -q || true
	cd services/payment-service && python -m pytest -q || true

config:
	docker compose config -q && echo "compose config OK"

# Prove the tests in a fix commit actually catch the bug: they must FAIL with the
# source rolled back to the parent commit and PASS with the fix applied.
# Default proves HEAD; override with `make prove REF=<sha>`.
# Runs in a throwaway worktree (prove_wt.sh) so it works on a dirty tree and leaves
# nothing behind — the fix must be COMMITTED, since a worktree checks out a ref.
prove:
	./scripts/prove_wt.sh $(REF)

# Regenerate docs/state.md -- the mutable half of the knowledge base (base tip, merged-PR
# ledger, ADR index, which CI jobs block). Everything on that page is read out of git, so
# it is never hand-edited; `kb-freshness` fails the build if the committed copy drifts.
# Durable prose stays in docs/kb.md, where `volatile-claim-lint` refuses decaying claims.
kb:
	./scripts/gen_state.sh
	./scripts/check_volatile_claims.sh
