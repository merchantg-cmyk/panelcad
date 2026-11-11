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
| PaddleOCR | Apache-2.0 |
| PaddleX | Apache-2.0 |
| ONNX Runtime | MIT |
| flatbuffers | Apache-2.0 |
| numpy | BSD-3-Clause |
| packaging | Apache-2.0 OR BSD-2-Clause |
| protobuf | BSD-3-Clause |

## Electrical OCR (Windows setup zip)

The Windows setup zip ships the default scan reader under
`resources/ocr/electrical`. That tree has the pinned ONNX weights, the
ONNX Runtime 1.31.1 Windows wheels, and the wheels that runtime requires
(flatbuffers 25.12.19, numpy 2.3.5, packaging 26.3, and protobuf 5.29.5).
PanelCAD does not download them on first scan and does not read `~/.paddlex`.

The weights are published PaddleOCR ONNX exports. Copyright PaddlePaddle
Authors. License: Apache-2.0. The models are PP-OCRv5 mobile detection,
English PP-OCRv5 mobile recognition, PP-OCRv6 small detection, PP-OCRv6
small recognition, and PP-LCNet text-line orientation. The Apache text is
`resources/ocr/electrical/licenses/paddleocr-Apache-2.0.txt`. The notice
next to it names each Hugging Face revision.

ONNX Runtime is MIT, Copyright Microsoft Corporation. The installer
installs the matching wheel from `resources/ocr/electrical/wheels` with
`pip --no-index --find-links`. The same folder holds flatbuffers
(Apache-2.0), numpy (BSD-3-Clause), packaging (Apache-2.0 OR BSD-2-Clause),
and protobuf (BSD-3-Clause), which that wheel requires. The MIT text is
`resources/ocr/electrical/licenses/onnxruntime-MIT.txt`. The other license
texts ship inside those wheels. The reader sets PaddleOCR's engine to
`onnxruntime`. It does not import or ship PaddlePaddle.

Hashes are pinned in `packaging/electrical-ocr.lock.json`. PanelCAD stays
AGPL-3.0-or-later. That does not relicense the weights, ONNX Runtime, or
those dependency wheels.

pytest is a test dependency (MIT). It is not part of a release install.

## Tesseract OCR runtime (Windows setup zip)

The Windows setup zip also ships a pinned x64 Tesseract 5.5.3 runtime under
`resources/ocr/tesseract`. It is a fallback when the electrical reader
cannot load. A scan does not require it. PanelCAD shells out to that
`tesseract.exe`. It is not linked into the Python package. The installer
does not change PATH and does not run a separate Tesseract installer.

The binaries come from the official GitHub CI installer
`tesseract-ocr-w64-setup-5.5.3.20260724.exe` (tag 5.5.3). That build is
MinGW. The zip contains the PE import closure (Tesseract, Leptonica, and
the codec and runtime DLLs those binaries load), not the training tools.
English recognition uses tessdata_fast 4.1.0 `eng.traineddata` (about
4.1 MB, Apache-2.0). tessdata_best is not included. `osd.traineddata` is
not included.

Tesseract and tessdata_fast are Apache-2.0. The Apache text and the
Tesseract authors file from that installer are
`resources/ocr/tesseract/licenses/tesseract/`. Leptonica is BSD-2-Clause.
The other DLLs and their licenses, including the LGPL libraries
(libiconv, libintl, libidn2, libunistring), the GCC runtime exception
(libgcc, libstdc++), and GPL-2.0-only JBIG-KIT (`libjbig-0.dll`, imported
by libtiff), are listed in `packaging/tesseract/NOTICE.md`. That notice
is copied next to the binaries as `licenses/NOTICE.md`. The DLLs are
dynamic. Replace a DLL in that folder to use another build with the same
exports.

Hashes are pinned in `packaging/tesseract-runtime.lock.json`. The setup
zip is built on a machine with 7-Zip, which unpacks the installer at
package time. A PC that only installs PanelCAD does not download
Tesseract.

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
