"""Troubleshooting and trace methods for :class:`panelcad.api.session.Session`."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionTroubleshootMixin:
    """Trace, checkpoint, cause-ranking, and troubleshooting log/export methods."""

    def trace_wire(self, number: str) -> dict[str, Any]:
        """Follow wire *number* across sheets: from/to, pages, devices."""

        from panelcad.core.xref_index import trace_wire

        return trace_wire(self.project, self.catalog, number)

    def circuit_for(self, ref: str) -> dict[str, Any]:
        """Control and power path for *ref*, from the drawing only."""

        from panelcad.core.troubleshoot import circuit_for

        return circuit_for(self.project, self.catalog, ref)

    def checkpoints(self, ref: str, symptom: str) -> dict[str, Any]:
        """Ordered LIVE and DE-ENERGIZED checks for *symptom* on *ref*."""

        from panelcad.core.troubleshoot import checkpoints

        return checkpoints(self.project, self.catalog, ref, symptom)

    def upstream(self, endpoint: str) -> dict[str, Any]:
        """Devices and wires from *endpoint* (``ref.pin``) toward the supply."""

        from panelcad.core.troubleshoot import upstream

        return upstream(self.project, self.catalog, endpoint)

    def downstream(self, endpoint: str) -> dict[str, Any]:
        """Devices and wires from *endpoint* (``ref.pin``) toward the load or return."""

        from panelcad.core.troubleshoot import downstream

        return downstream(self.project, self.catalog, endpoint)

    def plc_point(self, address: str) -> dict[str, Any]:
        """One PLC I/O point by address, point label, or tag on the drawing."""

        from panelcad.core.troubleshoot import plc_point

        return plc_point(self.project, self.catalog, address)

    def likely_causes(
        self,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Ranked causes. A first wrong reading narrows the fault to that segment."""

        from panelcad.core.troubleshoot import likely_causes

        return likely_causes(self.project, self.catalog, ref, symptom, readings)

    def export_troubleshoot(self, path: Path | str) -> Path:
        """Write the open ``troubleshoot.json`` document. Does not change the project file."""

        from panelcad.core.troubleshoot import write_troubleshoot

        return write_troubleshoot(self.project, self.catalog, path)

    def append_troubleshoot_log(
        self,
        *,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
        who: str = "",
        note: str = "",
        when: str = "",
        project_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Append a readings entry beside the project. The project file is not written."""

        from panelcad.core.troubleshoot import append_troubleshoot_log
        from panelcad.core.xref_index import QueryError

        src = project_path if project_path is not None else self.path
        if src is None:
            raise QueryError("save the project before writing the troubleshooting log")
        return append_troubleshoot_log(
            self.project,
            self.catalog,
            src,
            ref=ref,
            symptom=symptom,
            readings=readings,
            who=who,
            note=note,
            when=when,
        )

    def troubleshoot_guide(
        self,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Guided next check, fault window, and causes. Same payload as the dock."""

        from panelcad.core.troubleshoot_guide import guide

        return guide(self.project, self.catalog, ref, symptom, readings)

    def export_troubleshoot_csv(
        self,
        path: Path | str,
        *,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
        who: str = "",
        note: str = "",
    ) -> Path:
        """CSV of the guided session. Does not modify the project file."""

        from panelcad.core.troubleshoot_guide import session_rows, write_session_csv

        payload = self.troubleshoot_guide(ref, symptom, readings)
        return write_session_csv(path, session_rows(payload, who=who, note=note))

    def export_troubleshoot_pdf(
        self,
        path: Path | str,
        *,
        ref: str,
        symptom: str,
        readings: list[dict[str, Any]] | None = None,
        who: str = "",
        note: str = "",
    ) -> Path:
        """PDF of the guided session, opening with the lockout text."""

        from panelcad.render.troubleshoot_log import write_session_pdf

        payload = self.troubleshoot_guide(ref, symptom, readings)
        return write_session_pdf(
            path,
            payload,
            who=who,
            note=note,
            project_name=self.project.name,
        )
