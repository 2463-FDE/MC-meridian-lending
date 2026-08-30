"""No committed compose file may publish a datastore port to the host (D21b).

`docker-compose.yml` published `postgres:5432` and `redis:6379`. The Postgres half is
a credentialed bypass of the gateway trust boundary; the Redis half needs no credential
at all, because the base compose runs `redis:7-alpine` with no `requirepass` and
`REDIS_URL` carries no auth. Redis is not a cache of derived values here — the gateway
stores two bearer credentials in it (services/gateway/app/auth.py):

    session:{token}  -> {"id": .., "role": "admin"|"underwriter", ..}
    resume:{sid}     -> {"app_id": .., "token": <RAW continuation token>}

so `KEYS 'session:*'` yields a live officer session and `KEYS 'resume:*'` yields raw
continuation tokens — the bearer capability for the money-moving routes. That is the
exact value `origination-service/app/authz.py::hash_token` refuses to store in the clear
("a DB read / backup / logged row then yields only a non-replayable digest"): the
database-side control is undone by the cache-side storage the moment 6379 is reachable.

Keyed on the PRESENCE of a `ports:` key in the service block, not on its value, so every
spelling is covered — block list, flow sequence (`ports: ["5432:5432"]`), and long syntax.
`expose:` is unaffected and is what these services should carry instead.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATASTORES = ("postgres", "redis")

_SERVICES_KEY = re.compile(r"^services:\s*$")
# Keys may legally be quoted in YAML (`"ports":`), so both patterns accept quotes —
# an unquoted-only pattern is a fail-open hole in a check whose whole job is to refuse.
_SERVICE_NAME = re.compile(r"""^  ["']?([A-Za-z0-9._-]+)["']?:\s*$""")
_PORTS_KEY = re.compile(r"""^\s+["']?ports["']?:""")


def _compose_files() -> list[Path]:
    return sorted(ROOT.glob("docker-compose*.yml"))


def _service_blocks(path: Path) -> dict[str, list[tuple[int, str]]]:
    """{service name: [(1-based line number, text), ..]} for each 2-space-indented key
    under a top-level `services:`. A file with no `services:` mapping yields {}."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: dict[str, list[tuple[int, str]]] = {}
    in_services = False
    current: str | None = None
    for number, line in enumerate(lines, start=1):
        if _SERVICES_KEY.match(line):
            in_services, current = True, None
            continue
        if not in_services:
            continue
        # Any other column-0 key ends the services mapping.
        if line and not line[0].isspace():
            in_services, current = False, None
            continue
        match = _SERVICE_NAME.match(line)
        if match:
            current = match.group(1)
            blocks.setdefault(current, [])
            continue
        # A 2-space-indented key that is NOT a bare `name:` (e.g. `x-anchor: &a "v"`)
        # closes the current block without opening one.
        if line.startswith("  ") and not line.startswith("   ") and line.strip():
            current = None
            continue
        if current is not None and line.strip():
            blocks[current].append((number, line))
    return blocks


def test_the_datastore_services_are_still_named_what_this_test_grades():
    """Fail closed on a rename. Every assertion below is scoped to a service NAME, so
    renaming `postgres` to `db` would leave the port published and every case vacuously
    green — the same silently-eroding coverage the spec-diff-gate map rule exists for."""
    found = set()
    for path in _compose_files():
        found |= set(_service_blocks(path)) & set(DATASTORES)
    missing = sorted(set(DATASTORES) - found)
    assert not missing, (
        f"no compose file defines a service named {missing} — this test grades datastore "
        "port exposure BY SERVICE NAME, so it is now blind. Update DATASTORES."
    )


def test_compose_files_are_present_to_grade():
    assert _compose_files(), "no docker-compose*.yml found at the repo root"


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_no_datastore_service_publishes_a_host_port(path: Path):
    offenders = [
        f"{path.name}:{number}: {name} -> {text.strip()}"
        for name, block in _service_blocks(path).items()
        if name in DATASTORES
        for number, text in block
        if _PORTS_KEY.match(text)
    ]
    assert not offenders, (
        "a datastore service publishes a port to the host (D21):\n  "
        + "\n  ".join(offenders)
        + "\nUse `expose:` instead; reach the datastore with `docker compose exec`."
    )


# --- hermetic cases for the parser itself -------------------------------------
# The cases above grade the real compose files, so they say nothing about the
# forms this parser must not miss. These feed it synthetic documents instead,
# mirroring compose-hardening-gate-tests' hermetic approach: without them the
# patterns could regress to an unquoted-only or value-matching shape and every
# case above would stay green.

_FIXTURES = {
    "block list": '    ports:\n      - "5432:5432"\n',
    "flow sequence": '    ports: ["5432:5432"]\n',
    "long syntax": "    ports:\n      - target: 5432\n        published: 5432\n",
    "quoted key": '    "ports":\n      - "5432:5432"\n',
    "single-quoted key": "    'ports':\n      - \"5432:5432\"\n",
}


@pytest.mark.parametrize("form", sorted(_FIXTURES), ids=lambda f: f.replace(" ", "-"))
def test_every_way_of_publishing_a_port_is_caught(tmp_path, form):
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "services:\n  postgres:\n    image: postgres:16-alpine\n" + _FIXTURES[form],
        encoding="utf-8",
    )
    block = _service_blocks(path)["postgres"]
    assert any(_PORTS_KEY.match(text) for _, text in block), (
        f"a `ports:` key written as {form!r} is not detected — this parser would pass a "
        "compose file that publishes the port"
    )


@pytest.mark.parametrize("name", ['  postgres:\n', '  "postgres":\n', "  'postgres':\n"])
def test_a_quoted_service_name_is_still_recognised(tmp_path, name):
    path = tmp_path / "docker-compose.yml"
    path.write_text(f"services:\n{name}    image: x\n    ports:\n      - \"5432:5432\"\n", "utf-8")
    assert "postgres" in _service_blocks(path), (
        f"service key {name.strip()!r} not recognised — the datastore would go ungraded"
    )


def test_expose_is_not_mistaken_for_a_published_port(tmp_path):
    """The fix replaces `ports:` with `expose:`; a check that flagged both would be
    unsatisfiable and would get switched off rather than fixed."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        'services:\n  postgres:\n    image: x\n    expose:\n      - "5432"\n', encoding="utf-8"
    )
    block = _service_blocks(path)["postgres"]
    assert not any(_PORTS_KEY.match(text) for _, text in block)
