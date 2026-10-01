"""Firmware (固件) of York wire controllers: the updates the app offers.

Read-only: updates are installed from the 约克智慧家 app (see firmware.py).
"""

from __future__ import annotations

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import firmware
from .coordinator import YorkConfigEntry, YorkCoordinator
from .entity import YorkEntity, async_add_device_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, code: [YorkFirmware(coordinator, code)],
    )


class YorkFirmware(YorkEntity, UpdateEntity):
    """固件: the controller's MCU and Wi-Fi firmware and the updates the app offers.

    * installed_version: the controller record's versionMcu / versionWifi,
      the app's OTA page rows (e.g. "C-01603 / W-00258"), refreshed at each
      discovery (every 15 minutes).
    * latest_version: the same while the app offers no update; else a name
      for the offered updates (firmware.latest_version, e.g. "MCU OTA
      400004"), as the answer has no version number. Unknown until the
      first read (shortly after setup, then every 6 hours).
    * release_summary: the offered updates' release notes (otaDesc).
    * in_progress: while the controller reports otaFlg "1" (status
      polling; the app's "OTA升级中..."). Meanwhile the offer read before
      is kept (YorkCoordinator._fetch_firmware); without one (an update
      started from the app between two reads), the latest version is
      unknown rather than the installed one, which would read "up to
      date" while it installs.

    No install: Home Assistant never starts an update (no INSTALL feature,
    so the install service is refused); updates are installed from the app.
    """

    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # The category of update entities (UpdateEntityDescription's default);
    # without INSTALL, UpdateEntity itself would fall back to DIAGNOSTIC.
    _attr_entity_category = EntityCategory.CONFIG
    # PROGRESS: HA shows in_progress (else only its own installs count).
    _attr_supported_features = UpdateEntityFeature.PROGRESS

    def __init__(self, coordinator: YorkCoordinator, code: str) -> None:
        super().__init__(coordinator, code, "firmware")

    @property
    def installed_version(self) -> str | None:
        return firmware.installed_version(self.device.info)

    @property
    def latest_version(self) -> str | None:
        entry = self.device.firmware
        if entry is None:
            return None  # not read yet
        updates = firmware.offered(entry)
        if not updates and self.device.ota:
            # Installing an update the last read did not offer (e.g. the
            # app's popup, tapped between two reads): which one is unknown.
            return None
        return firmware.latest_version(self.installed_version, updates)

    @property
    def release_summary(self) -> str | None:
        return firmware.release_summary(firmware.offered(self.device.firmware))

    @property
    def in_progress(self) -> bool:
        return self.device.ota

    @property
    def entity_picture(self) -> str | None:
        """The integration's icon where Home Assistant serves it itself.

        UpdateEntity shows the brand icon of the integration: a path of
        Home Assistant's own (/api/brands/integration/…, which serves
        this custom integration's brand/icon.png) in newer releases, the
        brands CDN in older ones such as 2025.3. The CDN does not have
        this custom integration and would show its placeholder image, so
        there the entity shows its icon instead.
        """
        picture = super().entity_picture
        return picture if picture is not None and picture.startswith("/") else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """An offered update is newer.

        The latest version names the offered updates instead of giving a
        version number (there is none to compare); it differs from the
        installed one exactly while the app offers an update.
        """
        return True
