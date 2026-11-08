"""Benchmark electrical OCR, Tesseract, and Paddle on synthetic shop drawings.

The electrical engine is on-device. It does not call a cloud OCR API.
Tesseract and Paddle are optional baselines: if they are not installed, the
JSON records that and still scores the engines that did run.

Targets, and only these:
  MichellesPC       shop PC
  DESKTOP-3S1HOH0   home PC
Do not run this on Drew-PC or the Pixel 6 Pro. A Cursor cloud VM result is
not a local result. The JSON leaves MichellesPC and DESKTOP-3S1HOH0 empty
unless ``platform.node()`` is one of those names.

Working directory and command on either target PC
-------------------------------------------------
The usual PanelCAD bench tree, from
``.cursor/skills/verify-panelcad/scripts/setup-windows.ps1``::

    cd %USERPROFILE%\\panelcad-test\\src-<rev>
    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe scripts\\bench_electrical_ocr.py --out %USERPROFILE%\\panelcad-test\\ocr-bench.json

One-time bench install (does not touch the main PanelCAD install). ``<rev>``
is the folder name you extracted, without a ``src-`` prefix in ``-Rev``::

    powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\\panelcad-test\\src-<rev>\\.cursor\\skills\\verify-panelcad\\scripts\\setup-windows.ps1 -Zip %USERPROFILE%\\panelcad-test\\src-<rev>.zip

From a git checkout of this branch instead of a zip::

    cd %USERPROFILE%\\panelcad-test\\panelcad
    py -3.12 -m venv %USERPROFILE%\\panelcad-test\\venv
    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe -m pip install -e .
    cd %USERPROFILE%\\panelcad-test\\panelcad
    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe scripts\\bench_electrical_ocr.py --out %USERPROFILE%\\panelcad-test\\ocr-bench.json

Tests, from that same directory::

    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe -m pip install pytest
    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe -m pytest tests\\test_electrical_ocr.py

Tesseract (optional baseline, not bundled). Same places
``scripts/Install-PanelCAD.ps1`` already points at. Put ``tesseract`` on
PATH. English ``eng.traineddata`` is required.

    https://github.com/tesseract-ocr/tesseract
    https://tesseract-ocr.github.io/tessdoc/Downloads.html

Paddle OCR (optional baseline, not a PanelCAD dependency). If this install
fails, the harness records the error. The electrical engine still runs::

    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe -m pip install paddlepaddle paddleocr

A CPU wheel sometimes needs the Paddle index instead of PyPI::

    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe -m pip install paddlepaddle -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe -m pip install paddleocr
"""

from __future__ import annotations

from panelcad.ocr.bench import main

if __name__ == "__main__":
    raise SystemExit(main())
