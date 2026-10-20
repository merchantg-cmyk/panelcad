"""Process entry. The GUI splash is shown before the CLI module loads."""

from __future__ import annotations


def main() -> None:
    import sys

    from panelcad.gui.splash import (
        close_splash,
        command_is_gui,
        normalize_gui_platform,
        prepare_early_splash,
    )

    gui = command_is_gui(sys.argv)
    try:
        if gui:
            normalize_gui_platform()
            prepare_early_splash("Loading parts library...")
        from panelcad.cli.main import main as cli_main

        cli_main()
    except SystemExit:
        raise
    except BaseException:
        close_splash()
        raise
