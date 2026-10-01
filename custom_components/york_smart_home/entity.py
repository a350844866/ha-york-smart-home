"""Base entity for York Smart Home."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import as_number
from .const import DOMAIN, KEY_INDOOR_HUMIDITY
from .coordinator import YorkConfigEntry, YorkCoordinator, YorkDevice, YorkScene
from .profile import Profile
from .scenes import SceneType, scene_device_count
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


@callback
def async_add_scene_entities(
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[YorkCoordinator, YorkScene], Iterable[Entity]],
) -> None:
    """Add entities for the cloud scenes, also for ones created in the app later.

    Only for scenes the app shows (YorkScene.offered): one it hides gets
    its entity once the app would show it. A scene deleted (or hidden) in
    the app keeps its entity, which becomes unavailable (and available
    again if the scene comes back).
    """
    coordinator = entry.runtime_data
    known: set[tuple[int, str]] = set()

    @callback
    def add_new() -> None:
        new = [
            scene
            for home in coordinator.homes.values()
            for scene in home.scenes.values()
            if scene.present and scene.offered and (scene.home_id, scene.smart_id) not in known
        ]
        if new:
            known.update((scene.home_id, scene.smart_id) for scene in new)
            async_add_entities(e for scene in new for e in factory(coordinator, scene))

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class YorkSceneEntity(CoordinatorEntity[YorkCoordinator]):
    """One cloud scene (智能场景), on a device for its home.

    The name is the scene's name in the app (renaming it there renames the
    entity, not its entity id). Unavailable while the scene is gone, hidden
    by the app, or no longer of a type the app gives this entity's control
    (``_scene_control``: RUN for buttons, SWITCH for switches).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "scene"
    _scene_control: str

    def __init__(self, coordinator: YorkCoordinator, scene: YorkScene) -> None:
        super().__init__(coordinator)
        self._home_id = scene.home_id
        self._smart_id = scene.smart_id
        self._attr_unique_id = f"home_{scene.home_id}_scene_{scene.smart_id}"
        self._attr_name = scene.name
        home = coordinator.homes[scene.home_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"home_{scene.home_id}")},
            translation_key="home",
            translation_placeholders={"home": home.name or str(scene.home_id)},
            manufacturer="York 约克",
            model="约克智慧家",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def scene(self) -> YorkScene | None:
        home = self.coordinator.homes.get(self._home_id)
        return home.scenes.get(self._smart_id) if home is not None else None

    @property
    def stype(self) -> SceneType | None:
        """The scene's type, if it still has this entity's control (else None)."""
        scene = self.scene
        stype = scene.stype if scene is not None else None
        return stype if stype is not None and stype.control == self._scene_control else None

    @property
    def available(self) -> bool:
        home = self.coordinator.homes.get(self._home_id)
        scene = self.scene
        return (
            super().available
            and home is not None
            and home.present
            and scene is not None
            and scene.present
            and scene.offered
            and self.stype is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (scene := self.scene) is None or (stype := self.stype) is None:
            return None
        return {
            "scene_type": stype.key,
            "devices": scene_device_count(scene.record),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        if (scene := self.scene) is not None and scene.present:
            self._attr_name = scene.name  # renamed in the app
        super()._handle_coordinator_update()


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
