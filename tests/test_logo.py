from pathlib import Path

from panelcad import Session

# 1x1 transparent PNG
_MINI_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_logo_copied_and_embedded(tmp_path: Path):
    src = tmp_path / "brand.png"
    src.write_bytes(_MINI_PNG)
    s = Session.create("Logo Co")
    s.set_logo(src)
    project = tmp_path / "job" / "job.pcad.yaml"
    s.save(project)
    assert s.project.title_block.logo == "assets/company_logo.png"
    copied = project.parent / "assets" / "company_logo.png"
    assert copied.is_file()
    svg = tmp_path / "job" / "job.svg"
    s.export_svg(svg)
    text = svg.read_text(encoding="utf-8")
    assert "data:image/png;base64," in text
    assert "LOGO" not in text  # placeholder gone when image is present


def test_missing_logo_does_not_crash(tmp_path: Path):
    s = Session.create("No Logo")
    s.project.title_block.logo = "missing.png"
    svg = tmp_path / "out.svg"
    s.export_svg(svg)
    assert "LOGO" in svg.read_text(encoding="utf-8")
