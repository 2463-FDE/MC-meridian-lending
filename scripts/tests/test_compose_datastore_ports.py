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

Graded on the RESOLVED config, not the source text — see
scripts/check_compose_datastore_ports.py for why a line scan is fail-open. Keyed on the
PRESENCE of a `ports:` key in the resolved service mapping, not on its value, so every
spelling collapses to the same check — block list, flow sequence (`ports: ["5432:5432"]`),
long syntax, and a value inherited through an anchor. `expose:` is unaffected and is what
these services should carry instead.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_compose_datastore_ports as checker  # noqa: E402

DATASTORES = checker.DATASTORES


def _compose_files() -> list[Path]:
    return checker.compose_files(ROOT)


def test_the_datastore_services_are_still_named_what_this_test_grades():
    """Fail closed on a rename. Every assertion below is scoped to a service NAME, so
    renaming `postgres` to `db` would leave the port published and every case vacuously
    green — the same silently-eroding coverage the spec-diff-gate map rule exists for."""
    found = set()
    for path in _compose_files():
        found |= set(checker.services(path)) & set(DATASTORES)
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
        f"{path.name}: {name} -> ports: {value!r}"
        for name, value in checker.published(path).items()
    ]
    assert not offenders, (
        "a datastore service publishes a port to the host (D21):\n  "
        + "\n  ".join(offenders)
        + "\nUse `expose:` instead; reach the datastore with `docker compose exec`."
    )


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_no_datastore_service_hides_its_config_behind_extends(path: Path):
    """`extends:` pulls a mapping out of another file/service, which the loader here does
    not follow — an inherited `ports` would be invisible and the gate would pass over a
    published port. Refuse the construct instead of grading half a mapping."""
    unresolvable = checker.unresolvable(path)
    assert not unresolvable, (
        f"{path.name}: {unresolvable} use `extends:`, so this gate cannot see their full "
        "config and cannot prove the port is unpublished. Inline the mapping, or teach "
        "scripts/check_compose_datastore_ports.py to resolve `extends`."
    )


# --- hermetic cases for the resolver itself -----------------------------------
# The cases above grade the real compose files, so they say nothing about the
# forms this resolver must not miss. These feed it synthetic documents instead,
# mirroring compose-hardening-gate-tests' hermetic approach: without them the
# check could regress to a source-text scan and every case above would stay green.

_FIXTURES = {
    "block list": 'services:\n  postgres:\n    image: x\n    ports:\n      - "5432:5432"\n',
    "flow sequence": 'services:\n  postgres:\n    image: x\n    ports: ["5432:5432"]\n',
    "long syntax": (
        "services:\n  postgres:\n    image: x\n    ports:\n"
        "      - target: 5432\n        published: 5432\n"
    ),
    "quoted key": 'services:\n  postgres:\n    image: x\n    "ports":\n      - "5432:5432"\n',
    # The vector a source-text scan misses: Compose resolves the merge before it reads
    # the service, so 5432 is published with no `ports:` line in the postgres block.
    "merge key": (
        'x-pg: &pg\n  ports: ["5432:5432"]\n'
        "services:\n  postgres:\n    <<: *pg\n    image: x\n"
    ),
    # Compose's own merge tag: `yaml.safe_load` refuses it outright, which would make this
    # blocking gate a traceback rather than a verdict.
    "override tag": 'services:\n  postgres:\n    image: x\n    ports: !override ["5432:5432"]\n',
    # Same class, whole-service alias rather than a merge.
    "service alias": (
        'x-pg: &pg\n  image: x\n  ports: ["5432:5432"]\nservices:\n  postgres: *pg\n'
    ),
}


@pytest.mark.parametrize("form", sorted(_FIXTURES), ids=lambda f: f.replace(" ", "-"))
def test_every_way_of_publishing_a_port_is_caught(tmp_path, form):
    path = tmp_path / "docker-compose.yml"
    path.write_text(_FIXTURES[form], encoding="utf-8")
    assert checker.published(path), (
        f"a published port written as {form!r} is not detected — this gate would pass a "
        "compose file that publishes the port"
    )


@pytest.mark.parametrize(
    "name", ["  postgres:\n", '  "postgres":\n', "  'postgres':\n"]
)
def test_a_quoted_service_name_is_still_recognised(tmp_path, name):
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        f'services:\n{name}    image: x\n    ports:\n      - "5432:5432"\n', "utf-8"
    )
    assert "postgres" in checker.services(path), (
        f"service key {name.strip()!r} not recognised — the datastore would go ungraded"
    )


def test_expose_is_not_mistaken_for_a_published_port(tmp_path):
    """The fix replaces `ports:` with `expose:`; a check that flagged both would be
    unsatisfiable and would get switched off rather than fixed."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        'services:\n  postgres:\n    image: x\n    expose:\n      - "5432"\n',
        encoding="utf-8",
    )
    assert not checker.published(path)


def test_a_datastore_using_extends_is_refused(tmp_path):
    """Fail closed on the one construct this resolver cannot follow."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "services:\n  postgres:\n    extends:\n      file: base.yml\n      service: pg\n",
        encoding="utf-8",
    )
    assert checker.unresolvable(path) == ["postgres"]


def test_a_reset_tag_does_not_crash_the_loader(tmp_path):
    """`!reset` is the other Compose merge tag. It still counts as published — a reset is
    only meaningful over a base that published the port, which this gate already refuses —
    but it must produce that verdict, not a ConstructorError."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "services:\n  postgres:\n    ports: !reset null\n", encoding="utf-8"
    )
    # Graded on presence: a tagged scalar comes back as raw text (PyYAML applies no
    # implicit resolution under an explicit tag), and the value is not what this asks.
    assert "postgres" in checker.published(path)


def test_a_file_that_is_not_a_mapping_yields_no_services(tmp_path):
    """Fail closed through the rename check rather than raising AttributeError on `.get`."""
    path = tmp_path / "docker-compose.yml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert checker.services(path) == {}


def test_an_unparseable_file_is_refused_by_name(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text("services:\n---\nservices:\n", encoding="utf-8")
    with pytest.raises(
        ValueError, match="cannot prove the datastore ports are unpublished"
    ):
        checker.services(path)
