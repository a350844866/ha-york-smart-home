"""Binary sensors (fault, filter reminder, status banners) for York wire controllers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KEY_MODE_CONFLICT
from .control import filter_due, fresh_air_filters
from .coordinator import YorkConfigEntry
from .entity import YorkEntity, async_add_device_entities
from .profile import fresh_air
from .status import alarms, banner_on, has_banner, raw_alarm

PARALLEL_UPDATES = 0

# Translation keys spelled correctly (the status keys keep the app's typos).
_BANNER_TRANSLATION_KEYS = {
    KEY_MODE_CONFLICT: "mode_conflict",
    "auxiliay_heat_display": "auxiliary_heat_display",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, code: [
            YorkFault(coordinator, code),
            YorkFilterReminder(coordinator, code),
        ],
    )

    def banners(coordinator, code: str) -> list[YorkStatusBanner]:
        # The banners of the controller's page (colour / standard), and only
        # those whose key the controller reports: other models use other
        # keys, so a missing key is not "off".
        dev = coordinator.devices[code]
        view = dev.view
        return [
            YorkStatusBanner(coordinator, code, key, enabled)
            for key, enabled in dev.profile.banners.items()
            if has_banner(view, key)
        ]

    async_add_device_entities(entry, async_add_entities, banners, wait_for_status=True)


class YorkFault(YorkEntity, BinarySensorEntity):
    """On when any indoor unit reports an alarm code.

    Attributes: ``code`` and ``description`` of the first active alarm
    (only while there is one; no description for codes the app's table does
    not know), ``alarms`` (all active alarms) and ``raw_alarm``.
    """

    _attr_translation_key = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "fault")

    @property
    def is_on(self) -> bool | None:
        active = alarms(self.status)
        return None if active is None else bool(active)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.status
        active = alarms(status) or []
        attrs: dict[str, Any] = {}
        if active:
            first = active[0]
            attrs["code"] = first["code"]
            if "description" in first:
                attrs["description"] = first["description"]
        attrs["alarms"] = active
        # The app's red alarm icon, which also counts unused slots.
        attrs["raw_alarm"] = raw_alarm(status)
        return attrs


class YorkFilterReminder(YorkEntity, BinarySensorEntity):
    """On when the controller asks for the filter to be cleaned (allowShowLW).

    Colour page: 2 on any filter_life_tips slot, or 3 on slot 3; standard
    page: filter_life_tips1 == 2. A fresh-air controller's dialog shows
    each of its five filters: attribute ``filters`` (ok / clean / replace).
    """

    _attr_translation_key = "filter"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "filter")

    @property
    def is_on(self) -> bool | None:
        return filter_due(self.status, self.profile)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self.status
        if not fresh_air(status, self.profile):
            return None
        return {"filters": fresh_air_filters(status)}


class YorkStatusBanner(YorkEntity, BinarySensorEntity):
    """One of the control page's status banners (除霜中, 集中控制, ...)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, code: str, key: str, enabled: bool) -> None:
        super().__init__(coordinator, code, key)
        self._key = key
        self._attr_translation_key = _BANNER_TRANSLATION_KEYS.get(key, key)
        self._attr_entity_registry_enabled_default = enabled
        if key == KEY_MODE_CONFLICT:
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        return banner_on(self.status, self._key)
