# Electrical OCR shipped with PanelCAD

PanelCAD reads a scan with the on-device electrical reader. The weights,
the ONNX Runtime wheel, and the wheels that runtime requires are in this
setup folder. The installer does not change PATH, does not run a separate
OCR installer, and does not download these files on the shop PC. The first
scan does not contact Hugging Face, Paddle, or `~/.paddlex`.

Tesseract, when this zip also contains `resources/ocr/tesseract` or the PC
already has a `tesseract` command, is only a fallback for the case where
this reader cannot load. A scan does not need it.

## Weights

The ONNX files are the published PaddleOCR exports. Copyright PaddlePaddle
Authors. License: Apache-2.0. The Apache text is
`licenses/paddleocr-Apache-2.0.txt` next to this notice. PanelCAD stays
AGPL-3.0-or-later. That does not relicense these weights.

| Directory | Model | Hugging Face revision |
| --- | --- | --- |
| `models/PP-OCRv5_mobile_det_onnx` | PP-OCRv5 mobile detection | `e6f4fa85f00e168c862bc462aebca69eef9b3d3d` |
| `models/en_PP-OCRv5_mobile_rec_onnx` | English PP-OCRv5 mobile recognition | `3fafbc3b5dcf93dd72add9f48368be8a3a2cd33b` |
| `models/PP-OCRv6_small_det_onnx` | PP-OCRv6 small detection | `28fe5895c24fd108c19eb3e8479f4ab385fbfc62` |
| `models/PP-OCRv6_small_rec_onnx` | PP-OCRv6 small recognition | `b8f84f0b80c529de40b4fbb3544b84fa7233a513` |
| `models/PP-LCNet_x1_0_textline_ori_onnx` | PP-LCNet text-line orientation (0° / 180°) | `7fdcf3cf7061163eda7183b224aa334bd33068f7` |

The page pass uses the two PP-OCRv5 mobile models. One enlarged crop of tiny
glyphs above the title band uses the two PP-OCRv6 small models. Both passes
use the text-line orientation model because the reader turns that module on.
No other official model is loaded.

Hashes are pinned in `packaging/electrical-ocr.lock.json`. The setup zip is
built on a machine that downloads those exact revisions once, checks the
sha256, and packs the files. A PC that only installs PanelCAD does not
download them.

## ONNX Runtime

`wheels/` holds the pinned ONNX Runtime 1.31.1 Windows wheels (Python 3.11
through 3.14, x64 and ARM64) and the wheels that package requires:

| Wheel | Version | Tags |
| --- | --- | --- |
| flatbuffers | 25.12.19 | one pure-Python wheel for every tag above |
| numpy | 2.3.5 | the same Python and Windows tags as ONNX Runtime |
| packaging | 26.3 | one pure-Python wheel for every tag above |
| protobuf | 5.29.5 | one pure-Python wheel for every tag above |

The installer picks the ONNX Runtime wheel that matches the venv and installs
it with `pip --no-index --find-links` pointed at `wheels/`. Pip takes
flatbuffers, numpy, packaging, and protobuf from that same folder. It does
not download ONNX Runtime or those dependencies from PyPI. A scan does not
download them either.

ONNX Runtime is MIT, Copyright Microsoft Corporation. The MIT text is
`licenses/onnxruntime-MIT.txt`. flatbuffers is Apache-2.0. numpy is
BSD-3-Clause. packaging is Apache-2.0 OR BSD-2-Clause. protobuf is
BSD-3-Clause. Those license texts ship inside the wheels. The wheels are
not linked into PanelCAD. They are Python packages in the app-local
virtual environment.

PaddleOCR 3.7 and PaddleX are installed with the rest of PanelCAD's Python
packages. The reader sets `engine="onnxruntime"`. It does not import or ship
PaddlePaddle. Those packages are Apache-2.0. Their license text ships inside
the installed distributions.
