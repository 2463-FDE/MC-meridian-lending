"""Unit tests for the markdown chunker (spec D1.2)."""

from pathlib import Path

import pytest

from rag_eval.chunker import chunk_markdown

REPO = Path(__file__).resolve().parents[2]


def test_guidelines_sections_split():
    chunks = chunk_markdown(REPO / "policies" / "underwriting_guidelines.md")
    ids = [c.chunk_id for c in chunks]
    assert "underwriting_guidelines#eligibility" in ids
    assert "underwriting_guidelines#credit-decisioning" in ids
    assert "underwriting_guidelines#adverse-action-reg-b" in ids
    assert "underwriting_guidelines#debt-to-income-dti" in ids
    assert "underwriting_guidelines#records-retention" in ids


def test_fee_table_lands_in_intro_chunk():
    chunks = {
        c.chunk_id: c for c in chunk_markdown(REPO / "policies" / "fee_schedule.md")
    }
    intro = chunks["fee_schedule#_intro"]
    assert "Origination fee" in intro.text
    assert "Late payment fee" in intro.text
    assert "$25" in intro.text  # NSF row


def test_title_prefixed_for_context():
    chunks = chunk_markdown(REPO / "policies" / "fee_schedule.md")
    assert all(c.text.startswith("Meridian Lending — Fee Schedule") for c in chunks)


def test_ids_stable_and_unique():
    chunks = chunk_markdown(REPO / "policies" / "underwriting_guidelines.md")
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_synthetic_doc(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(
        "# Title\n\npreamble\n\n## Sec One\n\nbody\n\n## Empty\n\n## Sec Two\ntext\n"
    )
    chunks = chunk_markdown(p)
    ids = [c.chunk_id for c in chunks]
    assert ids == ["doc#_intro", "doc#sec-one", "doc#sec-two"]  # empty section dropped


def test_colliding_section_ids_raise(tmp_path):
    # Two distinct headings that slug to the same id would shadow one section's
    # content — the gold set references chunks by id, so this must fail loud.
    p = tmp_path / "doc.md"
    p.write_text("# T\n\n## Fee Schedule\naaa\n\n## Fee: Schedule!\nbbb\n")
    with pytest.raises(ValueError, match="duplicate chunk id"):
        chunk_markdown(p)


def test_symbol_only_heading_collides_with_intro_and_raises(tmp_path):
    # A symbol-only heading slugs to empty -> _intro, colliding with the real
    # intro chunk.
    p = tmp_path / "doc.md"
    p.write_text("# T\n\npreamble\n\n## ***\nbody\n")
    with pytest.raises(ValueError, match="duplicate chunk id"):
        chunk_markdown(p)


def test_hash_lines_inside_code_fence_are_not_headings(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# T\n\n## Real\nintro\n```\n## not a heading\nfake = 1\n```\nmore\n")
    chunks = chunk_markdown(p)
    ids = [c.chunk_id for c in chunks]
    assert ids == ["doc#real"]  # the fenced ## did not open a new section
    assert "## not a heading" in chunks[0].text  # fence content preserved
