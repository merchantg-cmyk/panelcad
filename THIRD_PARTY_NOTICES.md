# Third-party notices

PanelCAD is licensed under the GNU Affero General Public License, version 3
or any later version (`AGPL-3.0-or-later`). The text is `LICENSE`.

This file records components PanelCAD does not own. It does not relicense
them.

## PySide6 and Qt

The GUI uses PySide6, which loads Qt. The PySide6 wheel metadata for this
project's dependency is:

`LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`

PanelCAD is AGPL-3.0-or-later. That is compatible with calling Qt as an
LGPL library. PanelCAD does not static-link Qt into its own modules. A
source install and the Windows setup zip run `pip install`, which downloads
the PySide6 wheel. That wheel contains the Qt libraries and their license
files (`LICENSE.LGPL` and the rest of the Qt notices inside the installed
`PySide6` package).

**Relink.** You may replace the PySide6 and Qt libraries this program loads.
Install another build of PySide6 into the same environment (a wheel you
built, or `pip install --force-reinstall` of a build you trust) and start
`panelcad` again. The application does not keep a private copy of Qt inside
its own package. Object files for PanelCAD itself are the Python sources
under `src/panelcad/`.

## PyMuPDF

PDF import uses PyMuPDF. The wheel metadata is:

`Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License`

This distribution uses the AGPL-3.0 side. PanelCAD's "or any later version"
applies to PanelCAD. It does not change PyMuPDF's license. A separate
Artifex commercial license is not part of this repository.

## Permissive dependencies

These are installed from PyPI. This repository does not vendor their source.
If you ship a bundle that vendors them, keep their notices.

| Package | License |
| --- | --- |
| Pydantic | MIT |
| Pydantic AI | MIT |
| PyYAML | MIT |
| Typer | MIT |
| ezdxf | MIT |
| openpyxl | MIT |
| pypdf | BSD-3-Clause |
| Pillow | MIT-CMU |
| opencv-python-headless | Apache-2.0 |
| pytesseract | Apache-2.0 |

pytest is a test dependency (MIT). It is not part of a release install.

Tesseract OCR is a separate Apache-2.0 program. PanelCAD does not bundle it.

## Fonts

The application loads a system font (Arial, Segoe UI, DejaVu, or Liberation).
It does not embed a font in the package.

`tests/fonts/DejaVuSans.ttf` and `tests/fonts/DejaVuSans-Bold.ttf` are
unmodified Bitstream Vera / DejaVu fonts. `tests/fonts/LICENSE` is their
notice. Unmodified copies may be redistributed with that notice. A copy of
the font sold by itself is outside that permission.

## Icons

`src/panelcad/gui/icons/` includes Lucide icons under the ISC license. Some
of those icons derive from Feather, which is MIT. `src/panelcad/gui/icons/LICENSE`
is the notice. Keep it with the icons.

## Catalog data

`libraries/builtin/` is original YAML written for PanelCAD. Dimensions are
facts taken from public manufacturer cut sheets. No datasheet PDF is in this
repository. Manufacturer names are trademarks of their owners.
