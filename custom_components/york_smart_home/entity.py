"""Base entity for York Smart Home."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import as_number
from .const import DOMAIN, KEY_INDOOR_HUMIDITY
from .coordinator import YorkConfigEntry, YorkCoordinator, YorkDevice
from .profile import Profile
from .status import air_reading


@callback
def async_add_device_entities(
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[YorkCoordinator, str], Iterable[Entity]],
    *,
    wait_for_status: bool = False,
) -> None:
    """Add entities for current controllers and for ones discovered later.

    With ``wait_for_status`` a controller is only handled once a status
    report has arrived, for factories that create entities depending on the
    keys the controller reports.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = {
            code
            for code, dev in coordinator.devices.items()
            if code not in known and (dev.status or not wait_for_status)
        }
        if new:
            known.update(new)
            async_add_entities(e for code in new for e in factory(coordinator, code))

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class YorkEntity(CoordinatorEntity[YorkCoordinator]):
    """Entity bound to one wire controller."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: YorkCoordinator, code: str, key: str | None) -> None:
        super().__init__(coordinator)
        self._code = code
        self._attr_unique_id = f"{code}_{key}" if key else code
        info = self.device.info
        mac = info.get("macImei")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, code)},
            connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            name=info.get("xkqName") or f"York {code[-4:]}",
            manufacturer="York 约克",
            model=info.get("productName") or "Wi-Fi 线控器",
            sw_version=info.get("versionMcu"),
            hw_version=info.get("versionWifi"),
        )

    @property
    def device(self) -> YorkDevice:
        return self.coordinator.devices[self._code]

    @property
    def profile(self) -> Profile:
        """The app page's rules for this controller (colour / standard)."""
        return self.device.profile

    @property
    def status(self) -> dict[str, Any]:
        """Live status with the controller constants laid over it (YorkDevice.view)."""
        return self.device.view

    @property
    def available(self) -> bool:
        dev = self.device
        return super().available and dev.present and dev.online and bool(dev.status)


def indoor_temperature(status: dict[str, Any]) -> int | None:
    """Room temperature, or None if missing or implausible.

    The device reports it as an unsigned byte, so values above 128 are
    negative (the app does the same conversion).
    """
    value = as_number(status.get("indoor_temperature"))
    if value is None:
        return None
    value = int(value)
    if value > 128:
        value -= 256
    return value if -40 <= value <= 60 else None


def indoor_humidity(status: dict[str, Any], profile: Profile) -> float | None:
    """Room humidity as the fresh-air controller page shows it (else None)."""
    return air_reading(status, profile, KEY_INDOOR_HUMIDITY)
