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

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            slug = "_intro" if section == "_intro" else _slug(section)
            chunks.append(Chunk(f"{doc}#{slug}", doc, section, text))
        buf.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
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
