"""Check a release feed and hand an upgrade to the Windows installer."""

from panelcad.update.service import (
    UpdateError,
    check_for_update,
    install_update,
    rollback_update,
    startup_should_prompt,
)

__all__ = [
    "UpdateError",
    "check_for_update",
    "install_update",
    "rollback_update",
    "startup_should_prompt",
]
