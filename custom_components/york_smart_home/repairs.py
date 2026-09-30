"""Repair flows for York Smart Home.

The "session taken" issue can be fixed on purpose: logging in again takes
the session back from the other device (which is then logged out, as with
any login on this single-session account).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


class SessionTakenRepairFlow(RepairsFlow):
    """Log in again now instead of waiting for the yield period to end."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id or "")
            if (
                entry is None
                or entry.domain != DOMAIN
                or entry.state is not ConfigEntryState.LOADED
            ):
                return self.async_abort(reason="not_loaded")
            coordinator = entry.runtime_data
            try:
                await coordinator.async_reclaim_session()
            except HomeAssistantError:
                errors["base"] = "reclaim_failed"
            else:
                await coordinator.async_request_refresh()
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    def _placeholders(self) -> dict[str, str]:
        # Unlike HA's ConfirmRepairFlow, a custom flow does not get the
        # issue's placeholders ({minutes}) passed on by itself.
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        placeholders = dict(issue.translation_placeholders or {}) if issue else {}
        placeholders.setdefault("minutes", "?")
        return placeholders


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return SessionTakenRepairFlow(str((data or {}).get("entry_id") or ""))
