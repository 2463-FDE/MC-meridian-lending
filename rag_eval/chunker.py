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


def chunk_markdown(path: str | Path) -> list[Chunk]:
    path = Path(path)
    doc = path.stem
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
                    f"duplicate chunk id {chunk_id!r} in {path} — two sections "
                    f"slug to the same id (rename a heading so ids stay unique)"
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
