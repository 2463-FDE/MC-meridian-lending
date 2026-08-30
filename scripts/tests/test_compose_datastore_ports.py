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

`ports:` is not the only key that reaches the host, so `network_mode` is refused on a
graded datastore by the same presence rule: `host` puts the process in the host's network
namespace (listening on 5432/6379 there with no `ports:` entry needed or accepted), and
`service:x`/`container:x` put it in a namespace whose publishing this module cannot see.
A `driver: macvlan` or `external: true` network is the third route — a LAN address of the
datastore's own, carrying neither key — and is refused while the default driver is not, so
a private `backend` network still passes. All of it is graded over every file name Compose
would read, `compose.yaml` included, not only `docker-compose*.yml`.
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
def test_no_datastore_service_sets_a_network_mode(path: Path):
    """`ports:` is not the only key that reaches the host. `network_mode: host` puts the
    datastore process in the host's own network namespace, listening on 5432/6379 there
    with no `ports:` entry needed or accepted — a gate reading only `ports` goes green on
    exactly the D21 reachability it exists to remove. `service:x`/`container:x` hand the
    listener to another container's namespace, whose publishing this module cannot see.
    Every other value is what omitting the key already does, so the key is refused whole."""
    offenders = [
        f"{path.name}: {name} -> network_mode: {value!r}"
        for name, value in checker.network_modes(path).items()
    ]
    assert not offenders, (
        "a datastore service sets network_mode, so it is not on the compose network "
        "(D21):\n  "
        + "\n  ".join(offenders)
        + "\nRemove the key; `host` listens in the host namespace and `service:`/"
        "`container:` hides where it listens. Use `expose:` and reach it with "
        "`docker compose exec`."
    )


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_no_datastore_service_joins_an_unprovable_network(path: Path):
    """Third way off the compose network, after `ports` and `network_mode`: a top-level
    network with `driver: macvlan` puts the datastore on the physical LAN under its own
    address — D21's own "anyone on host/LAN" — while the service carries neither key. An
    `external: true` network is created outside this file, so its driver is unknowable."""
    offenders = [
        f"{path.name}: {name} -> {reason}"
        for name, reason in checker.unprovable_networks(path).items()
    ]
    assert not offenders, (
        "a datastore joins a network this file cannot prove is a private bridge "
        "(D21):\n  "
        + "\n  ".join(offenders)
        + "\nDeclare the network in this file with the default driver, or leave the "
        "datastore on the project's default network."
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


# The same hermetic treatment for `network_mode`, which reaches the host without ever
# naming a port. Without these the rule could regress to "postgres has no network_mode
# today" and every real-file case above would stay green, since neither committed compose
# file sets the key at all.

_NETWORK_MODE_FIXTURES = {
    # The vector the ports rule misses entirely: no `ports:` key exists or is accepted,
    # yet postgres listens on the host's 5432.
    "host direct": "services:\n  postgres:\n    image: x\n    network_mode: host\n",
    "host quoted": 'services:\n  postgres:\n    image: x\n    network_mode: "host"\n',
    "merge key": (
        "x-net: &net\n  network_mode: host\n"
        "services:\n  postgres:\n    <<: *net\n    image: x\n"
    ),
    "service alias": (
        "x-pg: &pg\n  image: x\n  network_mode: host\nservices:\n  postgres: *pg\n"
    ),
    "value alias": (
        "x-mode: &mode host\n"
        "services:\n  postgres:\n    image: x\n    network_mode: *mode\n"
    ),
    "override tag": (
        'services:\n  postgres:\n    image: x\n    network_mode: !override "host"\n'
    ),
    # One namespace removed: the listener lives in another container, whose publishing
    # this module cannot see.
    "shared namespace": (
        'services:\n  postgres:\n    image: x\n    network_mode: "service:gateway"\n'
    ),
}


@pytest.mark.parametrize(
    "form", sorted(_NETWORK_MODE_FIXTURES), ids=lambda f: f.replace(" ", "-")
)
def test_every_way_of_leaving_the_compose_network_is_caught(tmp_path, form):
    path = tmp_path / "docker-compose.yml"
    path.write_text(_NETWORK_MODE_FIXTURES[form], encoding="utf-8")
    assert checker.network_modes(path), (
        f"a network_mode written as {form!r} is not detected — this gate would pass a "
        "compose file whose datastore listens outside the compose network"
    )


def test_a_datastore_on_the_compose_network_is_not_flagged(tmp_path):
    """The satisfiable state: no `network_mode` key at all, which is what both committed
    compose files carry. A rule nothing can satisfy gets switched off rather than fixed."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        'services:\n  postgres:\n    image: x\n    expose:\n      - "5432"\n',
        encoding="utf-8",
    )
    assert not checker.network_modes(path)


_NETWORK_FIXTURES = {
    # A LAN address of its own, with no `ports` and no `network_mode` anywhere.
    "macvlan": (
        "networks:\n  lan:\n    driver: macvlan\n"
        "services:\n  postgres:\n    image: x\n    networks: [lan]\n"
    ),
    "ipvlan": (
        "networks:\n  lan:\n    driver: ipvlan\n"
        "services:\n  postgres:\n    image: x\n    networks:\n      - lan\n"
    ),
    # Created outside this file: nothing here can prove what it is.
    "external": (
        "networks:\n  lan:\n    external: true\n"
        "services:\n  postgres:\n    image: x\n    networks: [lan]\n"
    ),
    "external legacy mapping": (
        "networks:\n  lan:\n    external:\n      name: host\n"
        "services:\n  postgres:\n    image: x\n    networks: [lan]\n"
    ),
    # Mapping form of the service's own `networks` key, not a list.
    "mapping form": (
        "networks:\n  lan:\n    driver: macvlan\n"
        "services:\n  postgres:\n    image: x\n    networks:\n      lan:\n"
        "        aliases: [db]\n"
    ),
    # Declared nowhere in this file, so its driver is equally unknowable.
    "undeclared": "services:\n  postgres:\n    image: x\n    networks: [lan]\n",
}


@pytest.mark.parametrize(
    "form", sorted(_NETWORK_FIXTURES), ids=lambda f: f.replace(" ", "-")
)
def test_every_network_that_is_not_a_private_bridge_is_caught(tmp_path, form):
    path = tmp_path / "docker-compose.yml"
    path.write_text(_NETWORK_FIXTURES[form], encoding="utf-8")
    assert checker.unprovable_networks(path), (
        f"a network written as {form!r} is not detected — this gate would pass a compose "
        "file whose datastore is reachable from the host or the LAN"
    )


_HONEST_NETWORKS = {
    "no networks key": "services:\n  postgres:\n    image: x\n",
    "declared, default driver": (
        "networks:\n  backend: {}\nservices:\n  postgres:\n    image: x\n"
        "    networks: [backend]\n"
    ),
    "declared, driver bridge": (
        "networks:\n  backend:\n    driver: bridge\n"
        "services:\n  postgres:\n    image: x\n    networks: [backend]\n"
    ),
    "external false": (
        "networks:\n  backend:\n    external: false\n"
        "services:\n  postgres:\n    image: x\n    networks: [backend]\n"
    ),
}


@pytest.mark.parametrize(
    "form", sorted(_HONEST_NETWORKS), ids=lambda f: f.replace(" ", "-")
)
def test_a_private_bridge_network_is_not_flagged(tmp_path, form):
    """Both directions. Declaring a private `backend` network and putting the datastores
    on it is BETTER segmentation than this repo has today; a rule that refused it would be
    unsatisfiable and would get switched off rather than fixed."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(_HONEST_NETWORKS[form], encoding="utf-8")
    assert not checker.unprovable_networks(path)


@pytest.mark.parametrize(
    "name",
    [
        "compose.yaml",
        "compose.yml",
        "compose.override.yaml",
        "docker-compose.yaml",
        "docker-compose.override.yml",
    ],
)
def test_every_file_compose_would_read_is_graded(tmp_path, name):
    """Compose's default file names are compose.yaml, compose.yml, docker-compose.yaml and
    docker-compose.yml, in that PRECEDENCE order — a committed `compose.yaml` is read
    INSTEAD of `docker-compose.yml`. A `docker-compose*.yml` glob left the other three
    spellings ungraded, and the rename check could not notice: it unions across graded
    files, so `postgres` in docker-compose.yml kept it green while the ungraded file
    published 5432."""
    (tmp_path / name).write_text(
        'services:\n  postgres:\n    image: x\n    ports: ["5432:5432"]\n',
        encoding="utf-8",
    )
    graded = checker.compose_files(tmp_path)
    assert [p.name for p in graded] == [name], (
        f"{name} is not graded — Compose reads it, so a datastore port published there "
        "would never be seen"
    )
    assert checker.published(graded[0])


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
