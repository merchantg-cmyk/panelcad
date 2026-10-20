"""Logs, crash records, and bug reports.

The drawing tools stay on Session. This package records what happened.
A release can embed the team webhook; settings and environment variables
still override it, and the off switch stops sending.
"""

from panelcad.diag.paths import app_data_dir, logs_dir, reports_dir

__all__ = ["app_data_dir", "logs_dir", "reports_dir"]
