"""Markdown section chunker (spec D1.2, ledger DL-2).

One chunk per `##` section; content before the first `##` (minus the `#` title
line) becomes an `_intro` chunk — the fee_schedule table lives there. Chunk ids
are stable (`doc#section-slug`) so the gold query set can reference them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Chunk:
    chunk_id: str
    doc: str
    section: str
    text: str


def _slug(heading: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return s or "_intro"


def chunk_markdown(path: str | Path, doc_id: str | None = None) -> list[Chunk]:
    path = Path(path)
    # `doc_id` overrides the stem, and a manifest-admitting caller MUST pass one
    # (`run.corpus_doc_id`): on that path the filename is graded by nothing, so a
    # bare person name would become the officer-visible chunk id. The stem is used
    # only where admission is the lowercase-slug convention, which grades it.
    #
    # Lowercased so an approved corpus whose filenames are not slugs still yields
    # ids `run._CHUNK_ID` accepts — otherwise no gold `expected` entry could
    # reference them. Two documents sharing a stem collide here — case variants,
    # or the same stem in two directories. The callers reject duplicate ids after
    # chunking: `run()` aborts, and origination's policy_retrieval indexes
    # nothing.
    doc = doc_id if doc_id is not None else path.stem.lower()
    title = ""
    section = "_intro"
    buf: list[str] = []
    chunks: list[Chunk] = []

    seen: set[str] = set()

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            slug = "_intro" if section == "_intro" else _slug(section)
            chunk_id = f"{doc}#{slug}"
            # Gold queries reference chunks by id, so ids MUST be unique — two
            # headings that slug the same (or a symbol-only heading collapsing
            # to _intro) would silently shadow one section's content. Fail loud;
            # a colliding corpus is an authoring error, not a run to paper over.
            if chunk_id in seen:
                raise ValueError(
                    f"duplicate chunk id {chunk_id!r} — two sections slug to the "
                    "same id (rename a heading so ids stay unique). The path is "
                    "withheld: under manifest admission the filename is graded by "
                    "nothing and can itself be the identifier."
                )
            seen.add(chunk_id)
            chunks.append(Chunk(chunk_id, doc, section, text))
        buf.clear()

    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code  # a ## or # inside a fence is content, not a heading
            buf.append(line)
            continue
        if not in_code and line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if not in_code and line.startswith("## "):
            flush()
            section = line[3:].strip()
            continue  # heading text is carried by metadata + title prefix
        buf.append(line)
    flush()

    # Prefix doc title + section so section-less queries still match on doc
    # vocabulary ("fee schedule", "underwriting guidelines").
    for c in chunks:
        c.text = f"{title} — {c.section}\n{c.text}"
    return chunks
