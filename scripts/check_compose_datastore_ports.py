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


def compose_files(root: Path) -> list[Path]:
    return sorted(root.glob("docker-compose*.yml"))


def services(path: Path) -> dict[str, dict]:
    """{service name: resolved mapping} — anchors, aliases and `<<` merges applied.

    A service whose body is not a mapping (empty, or a scalar) yields {} so callers can
    treat every entry uniformly; the name still registers, which is what the rename check
    grades.
    """
    try:
        document = (
            yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}
        )
    except yaml.YAMLError as error:
        raise ValueError(
            f"{path.name}: not parseable as YAML, so this gate cannot prove the datastore "
            f"ports are unpublished — fix the file or teach this module to read it: {error}"
        ) from error
    block = document.get("services") or {} if isinstance(document, dict) else {}
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


def unresolvable(path: Path, names=DATASTORES) -> list[str]:
    """Graded services using `extends:`, whose full mapping lives in another file/service.

    Fail closed: this module resolves merge keys, not `extends`, so an inherited `ports`
    would be invisible here and the gate would go green over a published port.
    """
    found = services(path)
    return sorted(name for name in names if "extends" in found.get(name, {}))
