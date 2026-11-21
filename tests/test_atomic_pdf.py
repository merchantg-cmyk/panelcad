"""Windows-safe atomic PDF writing, metadata rewrite, and bookmark/link post-processing."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pypdf import PdfReader

from panelcad.api.session import Session
from panelcad.render import pdf as pdf_mod
from panelcad.render.pdf import atomic_pdf_path, project_to_pdf


def test_atomic_pdf_path_keeps_existing_file_on_error(tmp_path: Path) -> None:
    dest = tmp_path / "existing.pdf"
    dest.write_bytes(b"%PDF-1.4 original-bytes")

    with pytest.raises(RuntimeError, match="boom"):
        with atomic_pdf_path(dest) as tmp:
            tmp.write_bytes(b"%PDF-1.4 half-written")
            raise RuntimeError("boom")

    assert dest.read_bytes() == b"%PDF-1.4 original-bytes"
    assert list(tmp_path.iterdir()) == [dest]


def test_atomic_pdf_path_refuses_empty_output(tmp_path: Path) -> None:
    dest = tmp_path / "out.pdf"
    dest.write_bytes(b"%PDF-1.4 keep")

    with pytest.raises(OSError, match="empty"):
        with atomic_pdf_path(dest):
            pass

    assert dest.read_bytes() == b"%PDF-1.4 keep"
    assert list(tmp_path.iterdir()) == [dest]


def test_project_to_pdf_keeps_valid_pdf_if_metadata_embedding_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session.create("Atomic PDF")
    session.place("nema.contactors.coil", ref="CR1", x=60, y=40)
    dest = tmp_path / "drawing.pdf"

    def _fail_meta(pdf_path: Path, project, sheets) -> None:
        raise RuntimeError("simulated metadata write failure")

    monkeypatch.setattr(pdf_mod, "_embed_project_metadata", _fail_meta)
    project_to_pdf(session.project, session.catalog, dest)

    assert dest.is_file()
    assert dest.stat().st_size > 100
    reader = PdfReader(str(dest))
    assert len(reader.pages) == 1
    assert list(tmp_path.iterdir()) == [dest]


def test_project_to_pdf_embeds_metadata_without_in_place_truncation(tmp_path: Path) -> None:
    session = Session.create("Embedded Meta")
    session.place("nema.contactors.coil", ref="CR1", x=60, y=40)
    dest = tmp_path / "meta.pdf"

    project_to_pdf(session.project, session.catalog, dest)

    assert dest.is_file() and dest.stat().st_size > 100
    reader = PdfReader(str(dest))
    assert len(reader.pages) == 1
    assert reader.metadata is not None
    assert "/PanelCADProject" in reader.metadata
    assert list(tmp_path.iterdir()) == [dest]


def test_print_package_outlines_emit_no_pypdf_deprecation_and_leave_no_temps(tmp_path: Path) -> None:
    session = Session.create("Package Outlines")
    session.place("nema.contactors.coil", ref="CR1", x=60, y=40)
    dest = tmp_path / "package.pdf"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        info = session.print_package(dest, sections="cover,index,drawings")

    pypdf_deprecations = [
        str(w.message) for w in caught if "add_outline_item" in str(w.message)
    ]
    assert pypdf_deprecations == []
    assert info["outlines"] is True
    assert dest.is_file() and dest.stat().st_size > 100
    reader = PdfReader(str(dest))
    assert len(reader.outline) >= 1
    assert list(tmp_path.iterdir()) == [dest]
