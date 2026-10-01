"""Sensors for York wire controllers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    KEY_INDOOR_CO2,
    KEY_INDOOR_HUMIDITY,
    KEY_INDOOR_PM25,
    KEY_INDOOR_TVOC,
    RUNNING_STATES,
)
from .control import (
    has_simple_timer,
    timer_end_text,
    timer_end_time,
    timer_pending,
    timer_text_changes_at,
    timer_turns_on,
)
from .coordinator import YorkConfigEntry, uses_chinese
from .entity import YorkClockEntity, YorkEntity, async_add_device_entities, indoor_temperature
from .profile import fresh_air
from .status import air_reading, running_state

try:  # HA 2026.x: the old concentration constants are deprecated there (removal 2027.8)
    from homeassistant.const import UnitOfDensity, UnitOfRatio

    MICROGRAMS_PER_CUBIC_METER: str = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    PARTS_PER_MILLION: str = UnitOfRatio.PARTS_PER_MILLION
except ImportError:  # HA 2025.x
    from homeassistant.const import (
        CONCENTRATION_MICROGRAMS_PER_CUBIC_METER as MICROGRAMS_PER_CUBIC_METER,
        CONCENTRATION_PARTS_PER_MILLION as PARTS_PER_MILLION,
    )

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, code: [
            YorkIndoorTemperature(coordinator, code),
            YorkRunningState(coordinator, code),
        ],
    )

    def air_quality(coordinator, code: str) -> list[YorkAirQuality]:
        # Fresh-air colour controllers only (other controllers report 0 for
        # all of them), for every value the controller reports at all. An
        # implausible value (e.g. CO2 0 while the sensor warms up) reads as
        # unknown instead of hiding the sensor: this factory runs once per
        # controller, so a sensor left out here would not appear later.
        dev = coordinator.devices[code]
        view = dev.view
        if not fresh_air(view, dev.profile):
            return []
        return [
            YorkAirQuality(coordinator, code, description)
            for description in AIR_QUALITY
            if view.get(description.key) is not None
        ]

    async_add_device_entities(entry, async_add_entities, air_quality, wait_for_status=True)
    # Created once a status shows the controller reports the timer fields.
    async_add_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, code: (
            [YorkTimerEnd(coordinator, code)]
            if has_simple_timer(coordinator.devices[code].view)
            else []
        ),
        wait_for_status=True,
    )


# The fresh-air controller page's air-quality tiles (室内湿度 / PM2.5 / CO2 /
# TVOC). TVOC is a level without a unit (0 good, 1 medium, >= 2 bad: the
# tile's colour classes).
AIR_QUALITY: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=KEY_INDOOR_HUMIDITY,
        translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key=KEY_INDOOR_PM25,
        translation_key="indoor_pm25",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=MICROGRAMS_PER_CUBIC_METER,
    ),
    SensorEntityDescription(
        key=KEY_INDOOR_CO2,
        translation_key="indoor_co2",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PARTS_PER_MILLION,
    ),
    SensorEntityDescription(
        key=KEY_INDOOR_TVOC,
        translation_key="indoor_tvoc",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


class YorkAirQuality(YorkEntity, SensorEntity):
    """An air-quality tile of the fresh-air controller page.

    Unknown while the unit is off (the page shows "--") or the value is
    implausible.
    """

    def __init__(self, coordinator, code: str, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, code, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return air_reading(self.status, self.profile, self.entity_description.key)


class YorkIndoorTemperature(YorkEntity, SensorEntity):
    """Room temperature measured by the wire controller."""

    _attr_translation_key = "indoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "indoor_temperature")

    @property
    def native_value(self) -> int | None:
        return indoor_temperature(self.status)


class YorkRunningState(YorkEntity, SensorEntity):
    """What the app's home page overlays on the controller card.

    自清洁中 / 高温杀菌中 / 系统冷却中 / OTA升级中 / 人感舒适中 / 人感节能中,
    or idle when the card shows no overlay.
    """

    _attr_translation_key = "running_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(RUNNING_STATES)

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "running_state")

    @property
    def native_value(self) -> str:
        return running_state(self.status)


class YorkTimerEnd(YorkClockEntity, SensorEntity):
    """定时结束: when the simple timer ends and what it does then, as text.

    In Home Assistant's language and time zone (control.timer_end_text):
    "12:22 关机" / "Off at 12:22", "明天 08:00 开机" / "On at 08:00 tomorrow",
    "无定时" / "No timer" without a timer or once it has fired (over 10 minutes
    past its end, control.TIMER_FIRED_MS), "已设定定时" / "Timer set" while
    the end is not known. Attributes: ``end_time`` (ISO 8601, local time)
    when the end is known, ``action`` ("on" / "off", timerSimpleOnoff) while
    a timer is pending. A text, not a timestamp: it never reads "unknown"
    (and has a timer icon, icons.json, instead of the timestamp's clock).
    """

    _attr_translation_key = "timer_end"

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "timer_end")

    @property
    def native_value(self) -> str:
        return timer_end_text(self.status, self._now, uses_chinese(self.hass))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.status
        now = self._now
        if not timer_pending(status, now.timestamp() * 1000):
            return {}
        attrs: dict[str, Any] = {}
        if (end := timer_end_time(status, now)) is not None:
            attrs["end_time"] = end.isoformat(timespec="seconds")
        attrs["action"] = "on" if timer_turns_on(status) else "off"
        return attrs

    def _clock_change(self, now: datetime) -> datetime | None:
        # The day's name changes at midnight; the timer counts as fired.
        return timer_text_changes_at(self.status, now)
