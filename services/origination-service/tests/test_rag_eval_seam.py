"""The rag_eval import seam — card G2a (`docs/cards-week8-governance.md`).

`rag_eval/` is a repo-root package and origination is its only in-platform consumer. The
service's build context was `./services/origination-service`, so `rag_eval/` sat OUTSIDE the
context and never entered the image: `import rag_eval` failed in the container. That is a
files-not-in-the-image problem, not a `sys.path`/venv one, so the close is a repo-root build
context plus a `COPY` — not a `PYTHONPATH`, and never a copy of the modules into the service
(that would reproduce the per-service redactor duplication `redactor-drift` exists to police).

These tests pin the three edits so a later context or Dockerfile change cannot silently reopen
the seam. They read the real files and need no docker. The container-level proof — build the
image, import inside it — is `scripts/check_rag_eval_import.sh`, run by the blocking
`rag-eval-import-gate` CI job.
"""

import re
from pathlib import Path

# tests/ -> origination-service/ -> services/ -> repo root
REPO = Path(__file__).resolve().parents[3]


def _compose_service_block(name: str) -> str:
    """The lines of one service's stanza in the base compose file.

    Fails loudly when the stanza is absent: a verifier that finds nothing must not report
    success over a file it never checked.
    """
    lines = (REPO / "docker-compose.yml").read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line == f"  {name}:"]
    assert starts, f"no `  {name}:` stanza in docker-compose.yml"
    block = []
    for line in lines[starts[0] + 1 :]:
        if re.match(r"^  \S", line):  # next service key, same indent
            break
        block.append(line)
    assert block, f"`{name}` stanza in docker-compose.yml is empty"
    return "\n".join(block)


def _dockerignore_entries() -> list[str]:
    lines = (REPO / ".dockerignore").read_text().splitlines()
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]


def test_origination_builds_from_the_repo_root_context():
    block = _compose_service_block("origination-service")
    assert re.search(r"^\s+context:\s*\.\s*$", block, re.M), (
        "origination's build context must be the repo root, or rag_eval/ is outside it"
    )
    assert re.search(
        r"^\s+dockerfile:\s*services/origination-service/Dockerfile\s*$", block, re.M
    ), "a repo-root context needs the dockerfile path spelled out"
    assert not re.search(r"^\s+build:\s*\./services/", block, re.M), (
        "the service-directory build context is what excluded rag_eval/"
    )


def test_dockerfile_copies_rag_eval_next_to_the_service_app():
    dockerfile = (REPO / "services/origination-service/Dockerfile").read_text()
    assert re.search(r"^WORKDIR /app\s*$", dockerfile, re.M)
    # WORKDIR /app + rag_eval at ./rag_eval is the whole mechanism: cwd is on sys.path,
    # so `import rag_eval` resolves with no PYTHONPATH and no repo-wide venv.
    assert re.search(r"^COPY rag_eval \./rag_eval\s*$", dockerfile, re.M), (
        "the image must carry rag_eval/ itself — bind-mounting code would make the image "
        "compose-only"
    )
    assert re.search(
        r"^COPY services/origination-service/app \./app\s*$", dockerfile, re.M
    ), "a repo-root context means every COPY source is repo-relative"
    assert re.search(
        r"^COPY services/origination-service/requirements\.txt \.\s*$", dockerfile, re.M
    )


def test_dockerignore_keeps_env_out_of_the_repo_root_context():
    """.env holds POSTGRES_PASSWORD and is at the repo root — the new context's root.

    Every build context was a subdirectory before this change, so the root .dockerignore was
    inert and .env being absent from it cost nothing. It is live now.
    """
    entries = _dockerignore_entries()
    assert ".env" in entries
    assert ".env.*" in entries
    assert "!.env.example" in entries
    # The negation only re-includes if it comes after the patterns that excluded the file.
    assert entries.index("!.env.example") > entries.index(".env.*")


def test_dockerignore_does_not_exclude_the_package_being_copied():
    """A COPY of an excluded path fails loudly; a PARTIAL exclusion does not.

    `rag_eval/embedder.py` filtered out by a stray pattern would build an image whose
    `import rag_eval` succeeds and whose `rag_eval.index` raises at first use.
    """
    offenders = [
        entry
        for entry in _dockerignore_entries()
        if not entry.startswith("!")
        and (entry == "rag_eval" or entry.startswith("rag_eval/"))
        and entry != "rag_eval/.cache"
    ]
    assert offenders == [], f".dockerignore excludes part of rag_eval/: {offenders}"


def test_dockerignore_excludes_the_legacy_contaminated_dump():
    """kb_dump/applications.jsonl carries ssn/pan/dob (the ADR 0007 legacy raw dump).

    It never lands in the built image, but a root-context build still ships it to the
    daemon and its cache on every build unless .dockerignore excludes it.
    """
    assert "kb_dump" in _dockerignore_entries()
