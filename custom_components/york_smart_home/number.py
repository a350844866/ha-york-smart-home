"""Brightness settings (backlight, indicator light) for York wire controllers.

Colour controller (xkq-color-set): ion-range sliders 1..100, step 1,
disabled while "自动" (auto brightness) is on; a change sends the whole
six-key settings bundle. Standard controller (xkq-set): six backlight levels
(一档..六档, shown here as 1..6 and sent as their index 0..5) next to "自动";
a change sends its four-key bundle. No indicator light there.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import as_number
from .const import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    BRIGHTNESS_MODE_KEYS,
    DOMAIN,
    KEY_BACKLIGHT_BRIGHTNESS,
    KEY_BACKLIGHT_MODE,
    KEY_INDICATOR_BRIGHTNESS,
    STANDARD_BRIGHTNESS_LEVELS,
)
from .control import (
    auto_brightness,
    build_settings,
    build_standard_settings,
    has_color_settings,
    has_standard_settings,
    standard_brightness_level,
)
from .coordinator import YorkConfigEntry
from .entity import YorkEntity, async_add_device_entities

PARALLEL_UPDATES = 0  # the coordinator serializes commands per controller


@dataclass(frozen=True, kw_only=True)
class YorkNumberDescription(NumberEntityDescription):
    """A settings-page brightness slider."""

    # build_settings keyword for this slider.
    setting: str


NUMBERS: tuple[YorkNumberDescription, ...] = (
    YorkNumberDescription(
        key=KEY_BACKLIGHT_BRIGHTNESS,  # 背光亮度
        translation_key="backlight_brightness",
        setting="backlight",
    ),
    YorkNumberDescription(
        key=KEY_INDICATOR_BRIGHTNESS,  # 运行指示灯亮度
        translation_key="indicator_light_brightness",
        setting="indicator",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def create(coordinator, code: str) -> list[NumberEntity]:
        dev = coordinator.devices[code]
        view = dev.view
        if has_standard_settings(dev.xkq_type):
            # The mode key decides whether a level applies (see the colour
            # case below).
            if view.get(KEY_BACKLIGHT_MODE) is None or view.get(KEY_BACKLIGHT_BRIGHTNESS) is None:
                return []
            return [YorkBrightnessLevel(coordinator, code)]
        # Both mode keys are needed too: without them auto brightness always
        # reads as on (the app's rule), the auto switch is not created, and
        # the slider could never be written.
        if not has_color_settings(dev.xkq_type) or any(
            view.get(key) is None for key in BRIGHTNESS_MODE_KEYS
        ):
            return []
        return [
            YorkBrightness(coordinator, code, description)
            for description in NUMBERS
            if view.get(description.key) is not None
        ]

    async_add_device_entities(entry, async_add_entities, create, wait_for_status=True)


class YorkBrightness(YorkEntity, NumberEntity):
    """Backlight or indicator light brightness (colour controller)."""

    entity_description: YorkNumberDescription
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = BRIGHTNESS_MIN
    _attr_native_max_value = BRIGHTNESS_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, code: str, description: YorkNumberDescription) -> None:
        super().__init__(coordinator, code, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return as_number(self.status.get(self.entity_description.key))

    async def async_set_native_value(self, value: float) -> None:
        description = self.entity_description
        # Half up (round() would round 2.5 to 2); the slider has step 1.
        level = math.floor(value + 0.5)

        def build(view: dict[str, Any]) -> dict[str, Any]:
            # Checked again here: the state has just been re-read.
            _check_manual(view)
            if as_number(view.get(description.key)) == level:
                return {}  # already so: nothing is sent
            return build_settings(view, **{description.setting: level})

        _check_manual(self.status)
        # fresh: the bundle resends every setting, so build it from the
        # controller's current state like the settings page does.
        await self.coordinator.async_send_raw(self._code, build, fresh=True)


class YorkBrightnessLevel(YorkEntity, NumberEntity):
    """背光亮度 of the standard controller: level 1..6 (一档..六档).

    Unknown while auto brightness is on. The page has one selector for the
    six levels and "自动", so choosing a level also turns auto off (unlike
    the colour controller's slider, which is disabled while auto).
    """

    _attr_translation_key = "backlight_brightness"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = STANDARD_BRIGHTNESS_LEVELS
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, KEY_BACKLIGHT_BRIGHTNESS)

    @property
    def native_value(self) -> float | None:
        return standard_brightness_level(self.status)

    async def async_set_native_value(self, value: float) -> None:
        level = min(max(math.floor(value + 0.5), 1), STANDARD_BRIGHTNESS_LEVELS)

        def build(view: dict[str, Any]) -> dict[str, Any]:
            if standard_brightness_level(view) == level:
                return {}  # already so (and not auto): nothing is sent
            return build_standard_settings(view, auto=False, level=level)

        await self.coordinator.async_send_raw(self._code, build, fresh=True)


def _check_manual(status: dict[str, Any]) -> None:
    """The app disables the sliders while auto brightness is on.

    Refuse rather than switch auto off behind the user's back.
    """
    if auto_brightness(status):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="auto_brightness_on"
        )
