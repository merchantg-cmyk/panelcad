"""Export schematic, panel, and door DXF samples plus matplotlib previews."""

from __future__ import annotations

from pathlib import Path

from panelcad.examples.dol import build_dol

OUT = Path(__file__).resolve().parents[1] / "examples"


def _door(session) -> None:
    session.add_sheet("door", title="Door", kind="door_layout")
    session.set_title_block(description="Door operators and cutouts")
    session.place("iec.operators.estop", ref="S0", x=40, y=40, sheet_id="door")
    session.place("iec.operators.pb_nc", ref="S2", x=100, y=48, sheet_id="door")
    session.place("iec.operators.pb_no", ref="S1", x=140, y=48, sheet_id="door")
    session.place("iec.operators.pilot_light", ref="H1", x=180, y=48, sheet_id="door")


def _preview(dxf_path: Path, png_path: Path) -> None:
    import ezdxf
    import matplotlib

    matplotlib.use("Agg")
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration, TextPolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib.figure import Figure

    doc = ezdxf.readfile(dxf_path)
    fig = Figure(figsize=(11, 8), dpi=150)
    ax = fig.add_axes([0.03, 0.03, 0.94, 0.94])
    ctx = RenderContext(doc)
    config = Configuration(
        background_policy=BackgroundPolicy.CUSTOM,
        custom_bg_color="#f7f4ea",
        text_policy=TextPolicy.FILLING,
    )
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend, config=config).draw_layout(doc.modelspace(), finalize=True)
    fig.savefig(png_path, facecolor="#f7f4ea")


def main(dest: Path | None = None, previews: bool = True) -> list[Path]:
    dest = dest or OUT
    dest.mkdir(parents=True, exist_ok=True)
    session = build_dol()
    _door(session)
    names = {
        "power": dest / "dol_power.dxf",
        "enclosure": dest / "dol_enclosure.dxf",
        "door": dest / "dol_door.dxf",
    }
    written: list[Path] = []
    for sheet_id, path in names.items():
        session.export_dxf(path, sheet_id=sheet_id)
        written.append(path)
        if previews:
            png = path.with_name(path.stem + "_dxf.png")
            _preview(path, png)
            written.append(png)
    return written


if __name__ == "__main__":
    for path in main():
        print(path)
