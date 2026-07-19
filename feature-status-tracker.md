# Feature Status Tracker

| Feature | Branch | Base | Spec | ADRs | Teeth | Unit | Integration | Smoke | Date | Status |
|---------|--------|------|------|------|-------|------|-------------|-------|------|--------|
| Week 2 — RAG retrieval eval harness + corpus hygiene gate | `feature/rag-eval-impl-week2` | `main` | `docs/spec-rag-week2.md` | 0007, 0008 (merged via PR #5) | PASS (after fixes `5be22d6`) | 59 passed | passed (real corpus, in suite) | PASS (`scripts/smoke_rag_eval.sh`) | 2026-07-11 | PR-Raised |
| Week 3 — single-agent decisioning assistant + append-only decision record | `feature/decision-assistant-week3` | `main` | `docs/spec-decision-assistant-week3.md` | 0009 (implements 0008 contract) | PASS (after fixes `deb21b3`; was BLOCK: 2 High, 4 Med, 1 Low) | decision-service 48 passed, origination 199 passed | in suites (endpoint + agent-loop via FakeAdapter) | PASS vs live compose stack (decision→record→legacy→triggers→gate); real-key agent smoke NOT run (no CLAUDE_API_KEY; endpoints verified gated 503) | 2026-07-15 | PR-Raised |
