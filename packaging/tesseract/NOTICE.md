# Tesseract runtime shipped with PanelCAD

PanelCAD's Windows setup zip includes this folder so a PC with no system
Tesseract can run the raster scan reader. The installer does not change
PATH and does not run a separate Tesseract installer.

The bytes are the official Tesseract 5.5.3 Windows build
(`tesseract-ocr-w64-setup-5.5.3.20260724.exe` from
`tesseract-ocr/tesseract` tag 5.5.3, GitHub CI, NSIS). That build is
MinGW (libgcc, libstdc++, msvcrt). Upstream does not publish a separate
MSVC zip for 5.5.3. The file list is the PE import closure of
`tesseract.exe`, `libtesseract-5.dll`, and `libleptonica-6.dll`, plus the
DLLs those imports need. Training programs, cairo, pango, ICU, and glib
are inside the installer and are not shipped.

English recognition uses `tessdata_fast` tag 4.1.0 `eng.traineddata`
(4,113,088 bytes). That is the fast model. `tessdata_best` is a different,
larger model and is not included. This package does not claim it as an
accuracy upgrade. `osd.traineddata` is not included. The raster reader
uses PSM 6, 7, 8, and 11, which need `eng.traineddata`. PSM 0 orientation
already treats a missing OSD model as "no rotation".

`tessdata/configs/tsv` is the upstream one-line config
`tessedit_create_tsv 1`. pytesseract 0.3.10 and newer also passes
`-c tessedit_create_tsv=1` for `image_to_data`. The file stays in this
tessdata directory so that config name cannot be loaded from a system
tessdata tree. PanelCAD passes `--tessdata-dir` at the process that runs
this copy, so `TESSDATA_PREFIX` cannot replace the pinned English model.

Hashes for every file taken from the installer or from tessdata_fast are
in `packaging/tesseract-runtime.lock.json`.

These DLLs are loaded dynamically from this folder. You may replace any
of them with another build that exports the same symbols.

## Tesseract and the English model

| File | Project | License |
| --- | --- | --- |
| `tesseract.exe`, `libtesseract-5.dll` | Tesseract OCR 5.5.3 | Apache-2.0 |
| `tessdata/configs/tsv` | Tesseract OCR 5.5.3 | Apache-2.0 |
| `tessdata/eng.traineddata` | tessdata_fast 4.1.0 | Apache-2.0 |
| `licenses/tesseract/LICENSE` | Apache License 2.0 text from the installer | Apache-2.0 |
| `licenses/tesseract/AUTHORS` | Tesseract authors from the installer | Apache-2.0 |

## Leptonica

`libleptonica-6.dll` is Leptonica (BSD-2-Clause). Copyright (C) 2001-2020
Leptonica. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL ANY CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
SUCH DAMAGE.

## Other binaries

Versions below are the VERSIONINFO strings embedded in that DLL. A blank
version means the file had no product version; the sha256 in the lock is
the pin. Names and copyrights are from those resources or the upstream
project that ships that soname.

| File | What it is | License |
| --- | --- | --- |
| `libarchive-13.dll` | libarchive | BSD-2-Clause |
| `libb2-1.dll` | libb2 (BLAKE2) | CC0-1.0 |
| `libbrotlicommon.dll`, `libbrotlidec.dll` | Brotli | MIT |
| `libbz2-1.dll` | bzip2 | bzip2-1.0.6 |
| `libcurl-4.dll` | curl 8.21.0, Copyright (C) Daniel Stenberg | curl (MIT-style) |
| `libdeflate.dll` | libdeflate | MIT |
| `libexpat-1.dll` | Expat 2.8.2 | MIT |
| `libgcc_s_seh-1.dll` | GCC runtime | GPL-3.0-or-later WITH GCC-exception-3.1 |
| `libgif-7.dll` | giflib | MIT |
| `libiconv-2.dll` | GNU libiconv 1.19, Copyright (C) 1999-2026 Free Software Foundation | LGPL-2.1-or-later |
| `libidn2-0.dll` | GNU libidn2 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| `libintl-8.dll` | GNU gettext libintl, Copyright (C) 1995-2024 Free Software Foundation | LGPL-2.1-or-later |
| `libjbig-0.dll` | JBIG-KIT, pulled in by libtiff | GPL-2.0-only |
| `libjpeg-8.dll` | libjpeg (IJG / libjpeg-turbo soname 8) | IJG and BSD-3-Clause |
| `libLerc.dll` | Esri LERC, pulled in by libtiff | Apache-2.0 |
| `liblz4.dll` | LZ4 1.10.0, Copyright (C) 2013-2020 Yann Collet | BSD-2-Clause |
| `liblzma-5.dll` | XZ Utils 5.8.3, The Tukaani Project | 0BSD |
| `libopenjp2-7.dll` | OpenJPEG | BSD-2-Clause |
| `libpng16-16.dll` | libpng | libpng (zlib-style) |
| `libpsl-5.dll` | libpsl | MIT |
| `libsharpyuv-0.dll` | libwebp sharpyuv | BSD-3-Clause |
| `libssh2-1.dll` | libssh2 1.11.1, Copyright (C) The libssh2 project | BSD-3-Clause |
| `libstdc++-6.dll` | GCC libstdc++ | GPL-3.0-or-later WITH GCC-exception-3.1 |
| `libtiff-6.dll` | libtiff | libtiff (BSD-style) |
| `libunistring-5.dll` | GNU libunistring 1.4.2, Copyright (C) 2001-2019 Free Software Foundation | LGPL-3.0-or-later OR GPL-2.0-or-later |
| `libwebp-7.dll`, `libwebpmux-3.dll` | libwebp | BSD-3-Clause |
| `libwinpthread-1.dll` | MinGW-w64 winpthreads, Copyright (C) MinGW-W64 Project Members 2010-2023 | MIT |
| `libzstd.dll` | Zstandard | BSD-3-Clause OR GPL-2.0-only |
| `zlib1.dll` | zlib | Zlib |

`libgcc_s_seh-1.dll` and `libstdc++-6.dll` are covered by the GCC Runtime
Library Exception, so shipping them beside PanelCAD does not relicense
PanelCAD. `libiconv-2.dll`, `libintl-8.dll`, `libidn2-0.dll`, and
`libunistring-5.dll` are LGPL libraries loaded dynamically; replace the
DLL in this folder to relink. `libjbig-0.dll` is GPL-2.0-only JBIG-KIT as
shipped by the official Tesseract installer (libtiff imports it). It is
unmodified. `libzstd.dll` is the dual-licensed upstream build; PanelCAD
uses the BSD-3-Clause side.
