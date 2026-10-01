"""York Smart Home (约克智慧家) integration for Home Assistant."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import logging

from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Session, YorkApi, new_client_id
from .const import (
    CONF_CLIENT_ID,
    CONF_PHONE,
    CONF_RECLAIM_SESSION,
    CONF_SCAN_INTERVAL,
    CONF_SESSION,
    DEFAULT_RECLAIM_SESSION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_CLIENT_OUTDATED,
    ISSUE_SESSION_TAKEN,
)
from .coordinator import YorkConfigEntry, YorkCoordinator, issue_id

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.UPDATE,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: YorkConfigEntry) -> bool:
    _remove_fault_code_sensors(hass, entry)

    @callback
    def store_session(session: Session) -> None:
        """Persist renewed sessions so a restart does not need a new login."""
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SESSION: asdict(session)}
        )

    stored = entry.data.get(CONF_SESSION)
    api = YorkApi(
        async_get_clientsession(hass),
        entry.data[CONF_PHONE],
        password=entry.data.get(CONF_PASSWORD),
        auth=Session(**stored) if stored else None,
        client_id=entry.data[CONF_CLIENT_ID],
        on_session=store_session,
        reclaim_session=entry.options.get(CONF_RECLAIM_SESSION, DEFAULT_RECLAIM_SESSION),
    )
    coordinator = YorkCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    # Firmware information on its own slow timer, first read shortly after
    # setup (not part of it).
    coordinator.async_start_firmware_checks()
    return True


@callback
def _remove_fault_code_sensors(hass: HomeAssistant, entry: YorkConfigEntry) -> None:
    """Remove the entry's fault code sensors of 0.5.x from the entity registry.

    Their code and description are attributes of the fault binary sensor
    since 0.6.0. Every one of the entry, also of controllers not discovered
    now; nothing else.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == Platform.SENSOR
            and entity.platform == DOMAIN
            and entity.unique_id.endswith("_fault_code")
        ):
            _LOGGER.debug("Removing the old fault code sensor %s", entity.entity_id)
            registry.async_remove(entity.entity_id)


async def _async_entry_updated(hass: HomeAssistant, entry: YorkConfigEntry) -> None:
    # Also fires when a renewed session is stored, so only apply the options
    # instead of reloading the whole entry.
    coordinator = entry.runtime_data
    coordinator.update_interval = timedelta(
        seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    coordinator.api.reclaim_session = entry.options.get(
        CONF_RECLAIM_SESSION, DEFAULT_RECLAIM_SESSION
    )
    coordinator.async_apply_session_options()


async def async_unload_entry(hass: HomeAssistant, entry: YorkConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # The issues describe this entry's live state; a new setup recreates them.
        for key in (ISSUE_SESSION_TAKEN, ISSUE_CLIENT_OUTDATED):
            ir.async_delete_issue(hass, DOMAIN, issue_id(entry, key))
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: YorkConfigEntry) -> bool:
    """Migrate entries created by 0.1.x/0.2.0 (both stored as version 1)."""
    if entry.version > 2:
        return False  # downgrade from a future version
    if entry.version == 1:
        data = dict(entry.data)
        # 0.1.x kept a flat token/user_id and an optional push id; 0.2.0
        # already used client_id/session and only needs the version bump.
        token = data.pop("token", None)
        user_id = data.pop("user_id", None)
        data.pop("jg_reg_id", None)
        if CONF_SESSION not in data:
            data[CONF_SESSION] = (
                asdict(Session(token=token, user_id=int(user_id)))
                if token and user_id is not None
                else None
            )
        # 0.1.x reused whatever push id the user typed (often the phone's);
        # use our own from now on.
        data.setdefault(CONF_CLIENT_ID, new_client_id())
        data.setdefault(CONF_PASSWORD, None)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.debug("Migrated York entry %s to version 2", entry.entry_id)
    return True
