"""Policy retrieval inside the shipped container, driven by docker compose.

The unit vectors in test_policy_retrieval.py stub the embedder and read the
corpus out of the checkout. Neither can answer what the container answers:

  * does the IMAGE carry `rag_eval`, and does `corpus_dir()` resolve the
    `./policies` bind mount at /app/policies (WORKDIR /app), or does the service
    abstain on a corpus that is right there;
  * does `docker-compose.yml`'s own `${AWS_REGION:-}` default really arrive as
    "" -- the blank-env shape the region guard exists for -- rather than absent;
  * does a REAL botocore failure abstain, or does it leave `search()`? The unit
    vectors raise from fakes; only this file finds out which exception classes
    boto3 actually produces and whether the guard's `except` covers them.

That third question is not hypothetical. The guard originally wrapped
`make_embedder()` and `fit()`, and every unit vector passed. `BedrockEmbedder.fit`
is a no-op, so the first network call is the per-chunk `embed` in the index loop,
which sat outside it -- and this smoke raised
`ClientError(IncompleteSignatureException)` straight out of `search()`. A 500 on
the officer's request, found here and nowhere else.

NO CREDENTIALS are needed or used. The Bedrock cases are asserted on their
FAILURE being an abstention; a successful Titan call is not what is under test.

Skipped when docker is unavailable -- a developer without a daemon gets no
verdict rather than a false one. Set REQUIRE_CONTAINER_SMOKE=1 to turn every
skip reason into a failure, the way the CI step behind
test_migration_0020_live.py sets REQUIRE_LIVE_DB. There is no CI job on this file
yet; the flag is what one would set.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# When set, this file has no skip path at all: every skip below becomes a failure.
REQUIRE_CONTAINER_SMOKE = bool(os.getenv("REQUIRE_CONTAINER_SMOKE"))

REPO = Path(__file__).resolve().parents[3]

# The calibrated TF-IDF threshold from .env.example. Passed explicitly because
# compose defaults POLICY_RETRIEVAL_MIN_SCORE to "" and .env need not set it --
# the unset case is its own fail-closed behaviour, covered by the unit vectors.
THRESHOLD = "0.1609"

FEE_QUERY = "what is the origination fee"

# Printed as one line so it survives the service's log output on the same stream.
# `has_text` is a BOOL on purpose: the corpus text is the officer's to read, and a
# test does not need it echoed into CI output to assert it arrived.
_PROBE = """
import json
from app import policy_retrieval
try:
    a = policy_retrieval.search({query!r})
except Exception as exc:
    out = {{"raised": type(exc).__name__}}
else:
    out = {{
        "raised": None,
        "status": a.status,
        "reason": a.reason,
        "chunk_id": a.chunk_id,
        "score": round(a.score, 4),
        "has_text": bool(a.text),
        "tool_result": a.tool_result(),
        "corpus_dir": str(policy_retrieval.corpus_dir()),
    }}
print("SMOKE" + json.dumps(out))
"""


def _unavailable(reason: str):
    """Skip locally, fail in the gate that promised to run this."""
    if REQUIRE_CONTAINER_SMOKE:
        pytest.fail(
            f"REQUIRE_CONTAINER_SMOKE is set but the container smoke could not "
            f"run: {reason}. This checks policy retrieval in the shipped image; "
            "it must not pass without checking it."
        )
    pytest.skip(reason)


def _compose(*args, timeout):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def image():
    """Build the image under test once, so the smoke never grades a stale one."""
    if shutil.which("docker") is None:
        _unavailable("docker is not on PATH")
    probe = _compose("version", timeout=60)
    if probe.returncode != 0:
        _unavailable("the docker daemon is not reachable")
    # docker-compose.yml declares `env_file: .env`, and POSTGRES_PASSWORD has no
    # committed default, so compose refuses to interpolate the file without it --
    # for the whole project, even though retrieval touches no database. Name that
    # here: the bare compose error blames postgres for a missing setup step
    # (`cp .env.example .env`, per the README) and reads as a broken smoke.
    if not (REPO / ".env").is_file():
        _unavailable("no .env at the repo root — copy .env.example to .env")
    built = _compose("build", "origination-service", timeout=900)
    if built.returncode != 0:
        _unavailable(f"could not build origination-service: {built.stderr[-400:]}")
    yield


def _run(image, **env) -> dict:
    """One throwaway container, one `search()`, parsed back as a dict.

    `--no-deps` because retrieval touches no database, and `--rm` so nothing is
    left behind for the next run to trip over.
    """
    overrides = {"POLICY_RETRIEVAL_MIN_SCORE": THRESHOLD, **env}
    flags = []
    for key, value in overrides.items():
        flags += ["-e", f"{key}={value}"]
    proc = _compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        *flags,
        "origination-service",
        "python",
        "-c",
        _PROBE.format(query=FEE_QUERY),
        timeout=300,
    )
    lines = [
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if line.startswith("SMOKE")
    ]
    assert lines, (
        "the probe printed no result line — the container did not reach "
        f"`search()`.\nstdout:\n{proc.stdout[-800:]}\nstderr:\n{proc.stderr[-800:]}"
    )
    return json.loads(lines[-1][len("SMOKE") :])


def test_shipped_image_indexes_the_mounted_corpus(image):
    """The image carries rag_eval and finds the bind-mounted corpus."""
    result = _run(image, RAG_EMBEDDER="")
    assert result["raised"] is None
    assert result["corpus_dir"] == "/app/policies", (
        "corpus_dir() did not resolve the ./policies bind mount; retrieval would "
        "abstain on a corpus that is present"
    )
    assert result["status"] == "policy_hit", result
    assert result["chunk_id"].startswith("fee_schedule#")
    assert result["has_text"], "a hit must carry the passage the officer reads"
    # The boundary holds in the container too: chunk_id and text are officer-side.
    assert set(result["tool_result"]) == {"status", "score"}


def test_composes_blank_region_default_abstains(image):
    """`${AWS_REGION:-}` arrives as "", and blank is refused like absent.

    This is the shape a host with no AWS_REGION exported actually produces, and
    the reason `make_embedder` strips before handing the value to boto3.
    """
    result = _run(image, RAG_EMBEDDER="bedrock", AWS_REGION="")
    assert result["raised"] is None, (
        "a blank region left search() as an exception, which is a 500 on the "
        "officer's request"
    )
    assert result["status"] == "policy_abstain"
    assert result["reason"] == "corpus_unavailable"
    assert not result["has_text"]


def test_real_botocore_failure_abstains_rather_than_raising(image):
    """A real boto3 call with a real region and no credentials.

    Region configured, credentials absent/expired/rotated is the realistic
    production shape. botocore raises from inside the per-chunk embed loop, and
    the class it raises is botocore's, not a fake's — which is the only reason
    this case is worth a container.
    """
    result = _run(image, RAG_EMBEDDER="bedrock", AWS_REGION="us-east-1")
    assert result["raised"] is None, (
        f"botocore raised {result['raised']} out of search(); retrieval is an "
        "optional tool and must abstain, not 500 the officer's request"
    )
    assert result["status"] == "policy_abstain"
    assert result["reason"] == "corpus_unavailable"
