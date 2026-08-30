"""Resolved-config reader for the D21 datastore-port gate.

Compose resolves YAML anchors, aliases and `<<` merge keys BEFORE it ever looks at a
service, so a scan of the raw source text for a literal `ports:` line inside the service
block is fail-open:

    x-pg: &pg
      ports: ["5432:5432"]
    services:
      postgres:
        <<: *pg

publishes 5432 to the host while no `ports:` key appears anywhere in the postgres block.
The idiom is already in this repo (docker-compose.demo.yml defines `x-demo-internal-token`
as an anchor), so this is a form a future edit reaches for, not a hypothetical one.

`ports:` is also not the only key that puts a datastore on the host. Under
`network_mode: host` the container shares the host's network namespace, so the postgres
process listens on the host's 5432 directly — no `ports:` entry is needed, or even
accepted. A rule that grades only `ports` goes green on exactly the reachability this gate
exists to remove. `network_mode: "service:x"` / `"container:x"` are the same class one step
removed: the listener lives in another container's namespace, whose publishing this module
cannot see. So a graded datastore may not set `network_mode` AT ALL — every value either
exposes it (`host`), hides where it listens (`service:`/`container:`), or is the behaviour
omitting the key already gives (`bridge`, `none`, `default`). Keying on the presence of the
key keeps one rule instead of a value allowlist that has to stay correct.

Nor is `network_mode` the last of them. A top-level network with `driver: macvlan` gives
the datastore its own address on the physical LAN — "anyone on host/LAN", which is D21's
own wording — while the service carries neither `ports` nor `network_mode`, and an
`external: true` network is created outside this file so nothing here can prove what it
is. `unprovable_networks()` refuses both and accepts the default driver, so declaring a
private `backend` network and putting the datastores on it still passes: a rule that
refused `networks:` outright would refuse better segmentation than this repo has today,
and an unsatisfiable rule gets switched off rather than fixed.

All of which is graded over the files Compose would actually read — see `compose_files()`,
whose earlier `docker-compose*.yml` glob left `compose.yaml` (Compose's FIRST-choice
default name) entirely ungraded.

PyYAML performs the same resolution Compose does for anchors/aliases/merge keys, which is
why the gate reads the loaded mapping rather than the file's lines. What it does NOT
resolve is `extends:` (Compose reads another file/service for that), so a datastore
carrying `extends` is refused outright by `unresolvable()` rather than graded on a mapping
this module cannot see all of.

Two loader details keep a blocking gate producing a VERDICT rather than a traceback, since
an unexplained CI break is switched off rather than fixed:

* `!override` / `!reset` are Compose's own merge tags, and `yaml.safe_load` refuses them
  ("could not determine a constructor for the tag"). `_ComposeLoader` resolves an unknown
  `!tag` to the node underneath it, so `ports: !override [..]` is still seen as a published
  port. `ports: !reset null` is seen as one too — deliberately: a reset is only meaningful
  over a base that published the port, and that base is what this gate refuses.
* A file that parses to something other than a mapping yields no services, which the
  rename check then reports by name ("this test grades port exposure BY SERVICE NAME, so
  it is now blind") instead of raising `AttributeError` from a `.get` on a list.
"""

from pathlib import Path

import yaml

DATASTORES = ("postgres", "redis")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Compose's `!override` / `!reset` merge tags."""


def _unknown_tag(loader, tag_suffix, node):
    """Resolve any `!tag` to the node it decorates, so a tagged value is still graded."""
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _unknown_tag)


_COMPOSE_GLOBS = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
)


def compose_files(root: Path) -> list[Path]:
    """Every committed file Compose would read, not just the one spelling in use here.

    Compose's default file names are `compose.yaml`, `compose.yml`, `docker-compose.yaml`
    and `docker-compose.yml`, IN THAT PRECEDENCE ORDER — a committed `compose.yaml`
    is read INSTEAD of `docker-compose.yml`, not alongside it. A glob of
    `docker-compose*.yml` alone leaves the three other spellings (and every
    `*.override.*` of them) ungraded, and the rename check cannot notice: it unions
    across graded files, so `postgres` found in `docker-compose.yml` keeps it green
    while an ungraded `compose.yaml` publishes 5432.
    """
    return sorted({path for pattern in _COMPOSE_GLOBS for path in root.glob(pattern)})


def _document(path: Path) -> dict:
    """The resolved top-level mapping, or {} for a file that is not one."""
    try:
        document = (
            yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}
        )
    except yaml.YAMLError as error:
        raise ValueError(
            f"{path.name}: not parseable as YAML, so this gate cannot prove the datastore "
            f"ports are unpublished — fix the file or teach this module to read it: {error}"
        ) from error
    return document if isinstance(document, dict) else {}


def services(path: Path) -> dict[str, dict]:
    """{service name: resolved mapping} — anchors, aliases and `<<` merges applied.

    A service whose body is not a mapping (empty, or a scalar) yields {} so callers can
    treat every entry uniformly; the name still registers, which is what the rename check
    grades.
    """
    block = _document(path).get("services") or {}
    block = block if isinstance(block, dict) else {}
    return {
        str(name): (body if isinstance(body, dict) else {})
        for name, body in block.items()
    }


def published(path: Path, names=DATASTORES) -> dict[str, object]:
    """{datastore name: its resolved `ports` value} for every graded service that has one.

    Keyed on the PRESENCE of the key, not its value: every spelling Compose accepts (block
    list, flow sequence, long syntax) lands on the same resolved key, and an empty list is
    still an edit that reintroduced the field.
    """
    found = services(path)
    return {
        name: found[name]["ports"] for name in names if "ports" in found.get(name, {})
    }


def network_modes(path: Path, names=DATASTORES) -> dict[str, object]:
    """{datastore name: its resolved `network_mode`} for every graded service that sets one.

    Keyed on the PRESENCE of the key, like `published()`, and for the same reason: `host`
    is a listener in the host's own namespace, `service:x` / `container:x` is a listener in
    a namespace this module cannot grade, and every remaining value is what omitting the
    key already does. None of them belong on a datastore, so the rule needs no taxonomy of
    values — only the fact that one is set. That also covers the spellings `published()`
    already survives (merge key, whole-service alias, `!override`), since both read the
    same resolved mapping.
    """
    found = services(path)
    return {
        name: found[name]["network_mode"]
        for name in names
        if "network_mode" in found.get(name, {})
    }


def _joined_networks(body: dict) -> list[str]:
    """The network names a service body joins. Compose accepts a list or a mapping."""
    joined = body.get("networks")
    if isinstance(joined, dict):
        return [str(name) for name in joined]
    if isinstance(joined, list):
        return [str(name) for name in joined]
    if isinstance(joined, str):
        return [joined]
    return []


def unprovable_networks(path: Path, names=DATASTORES) -> dict[str, str]:
    """{datastore name: why the network it joins is not provably a private bridge}.

    `network_mode` is not the only way off the compose network. A top-level network with
    `driver: macvlan` (or `ipvlan`) gives the container its own address on the physical
    LAN, which is precisely the "anyone on host/LAN" reachability D21 is about, and the
    datastore carries no `ports` and no `network_mode` while doing it. `external: true`
    is the same problem one level of indirection out: the network is created outside this
    file, so nothing here can prove it is not the host or a LAN bridge.

    Only the default driver is accepted, so the normal hardening move — declaring a
    private `backend` network and putting the datastores on it — still passes. A rule
    that refused `networks:` outright would refuse better segmentation than the file has
    today, and an unsatisfiable rule gets switched off rather than fixed.
    """
    found = services(path)
    block = _document(path).get("networks") or {}
    declared = block if isinstance(block, dict) else {}

    offenders: dict[str, str] = {}
    for name in names:
        for joined in _joined_networks(found.get(name, {})):
            definition = declared.get(joined)
            if joined not in declared:
                reason = f"joins undeclared network {joined!r}"
            elif not isinstance(definition, dict):
                reason = f"network {joined!r} has no readable definition"
            elif definition.get("external"):
                reason = f"network {joined!r} is external, so its driver is unknowable"
            elif "driver" in definition and str(definition["driver"]) != "bridge":
                reason = f"network {joined!r} uses driver {definition['driver']!r}"
            else:
                continue
            offenders[name] = reason
            break
    return offenders


def unresolvable(path: Path, names=DATASTORES) -> list[str]:
    """Graded services using `extends:`, whose full mapping lives in another file/service.

    Fail closed: this module resolves merge keys, not `extends`, so an inherited `ports`
    would be invisible here and the gate would go green over a published port.
    """
    found = services(path)
    return sorted(name for name in names if "extends" in found.get(name, {}))
