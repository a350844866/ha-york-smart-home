"""Climate entity for York wire controllers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import deflectors
from .api import is_on
from .const import (
    BASIC_MODES,
    DOMAIN,
    KEY_HALF_DEGREE_SUPPORTED,
    KEY_POWER,
    LOCK_FAN,
    LOCK_MODE,
    LOCK_TEMP,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_FLOOR_HEAT,
    MODE_GROUP,
    MODE_HEAT,
)
from .control import (
    current_fan,
    current_humidity_setpoint,
    current_mode,
    current_temperature_setpoint,
    has_fan,
    has_temperature,
    humidity_control,
    supported_fans,
    supported_modes,
    temp_limits,
)
from .coordinator import YorkConfigEntry
from .entity import YorkEntity, async_add_device_entities, indoor_humidity, indoor_temperature

PARALLEL_UPDATES = 0  # the coordinator serializes commands per controller

# Each mode is shown as the HVAC mode of its group (see const.MODE_GROUP);
# the specific mode is the preset.
_HVAC_MODES = {
    MODE_COOL: HVACMode.COOL,
    MODE_HEAT: HVACMode.HEAT,
    MODE_FAN: HVACMode.FAN_ONLY,
    MODE_DRY: HVACMode.DRY,
    MODE_AUTO: HVACMode.AUTO,
}
_GROUP_BY_HVAC = {v: k for k, v in _HVAC_MODES.items()}


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_device_entities(
        entry, async_add_entities, lambda coordinator, code: [YorkClimate(coordinator, code)]
    )


class YorkClimate(YorkEntity, ClimateEntity):
    """York air conditioner controlled through its Wi-Fi wire controller."""

    _attr_name = None
    _attr_translation_key = "york"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, None)

    # ------------------------------------------------------------ capability

    def _locked(self, key: str) -> bool:
        return is_on(self.status.get(key))

    @property
    def _york_mode(self) -> str | None:
        return current_mode(self.status, self.profile)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        groups = {MODE_GROUP[m] for m in supported_modes(self.status, self.profile)}
        return [HVACMode.OFF, *(_HVAC_MODES[g] for g in BASIC_MODES if g in groups)]

    @property
    def preset_modes(self) -> list[str] | None:
        # Only worth showing when the controller has more than the basic
        # modes (the author's cool/heat/fan/dry unit shows no presets).
        modes = supported_modes(self.status, self.profile)
        return modes if any(m not in BASIC_MODES for m in modes) else None

    @property
    def fan_modes(self) -> list[str] | None:
        if not has_fan(self._york_mode):
            return None
        # getWindArr (the colour page drops an "auto"-only list).
        return supported_fans(self.status, self.profile) or None

    @property
    def _humidity(self) -> tuple[str, tuple[int, int]] | None:
        return humidity_control(self.status, self._york_mode, self.profile)

    @property
    def _deflectors(self) -> deflectors.Layout | None:
        """The direction list, while the page shows its direction button."""
        lay = deflectors.layout(self.status, self.profile)
        # allowShowDirectionBtn: not in floor heating.
        return None if lay is None or self._york_mode == MODE_FLOOR_HEAT else lay

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self.fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        # The app also sets the temperature in fan mode (cooling range).
        if has_temperature(self.status, self._york_mode, self.profile):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._humidity is not None:
            features |= ClimateEntityFeature.TARGET_HUMIDITY
        if self.preset_modes:
            features |= ClimateEntityFeature.PRESET_MODE
        if self._deflectors is not None:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def target_temperature_step(self) -> float:
        # The wire controller pages hardcode a 0.5 step; only an explicit
        # "not available" flag limits it to whole degrees.
        return 1.0 if str(self.status.get(KEY_HALF_DEGREE_SUPPORTED)) == "0" else 0.5

    @property
    def min_temp(self) -> float:
        return temp_limits(self.status, self._york_mode, self.profile)[0]

    @property
    def max_temp(self) -> float:
        return temp_limits(self.status, self._york_mode, self.profile)[1]

    @property
    def min_humidity(self) -> float:
        humidity = self._humidity
        return humidity[1][0] if humidity else super().min_humidity

    @property
    def max_humidity(self) -> float:
        humidity = self._humidity
        return humidity[1][1] if humidity else super().max_humidity

    @property
    def swing_modes(self) -> list[str] | None:
        """Deflector 1 (up/down on 3D units): auto, levels 1..max, 3D / far wind.

        With unified control only, this sets every deflector at once.
        """
        lay = self._deflectors
        if lay is None:
            return None
        return deflectors.swing_options(self.profile, lay, self._york_mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mode_locked": self._locked(LOCK_MODE),
            "temperature_locked": self._locked(LOCK_TEMP),
            "fan_locked": self._locked(LOCK_FAN),
        }

    # ----------------------------------------------------------------- state

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = self.status.get(KEY_POWER)
        if is_on(power):
            mode = self._york_mode
            return _HVAC_MODES[MODE_GROUP[mode]] if mode else None
        if power in (0, "0", False):
            return HVACMode.OFF
        return None

    @property
    def preset_mode(self) -> str | None:
        return self._york_mode if self.preset_modes else None

    @property
    def fan_mode(self) -> str | None:
        return current_fan(self.status)

    @property
    def current_temperature(self) -> float | None:
        return indoor_temperature(self.status)

    @property
    def target_temperature(self) -> float | None:
        return current_temperature_setpoint(self.status)

    @property
    def current_humidity(self) -> float | None:
        # Only the fresh-air controller page shows the room humidity.
        return indoor_humidity(self.status, self.profile)

    @property
    def target_humidity(self) -> float | None:
        return current_humidity_setpoint(self.status) if self._humidity else None

    @property
    def swing_mode(self) -> str | None:
        lay = self._deflectors
        if lay is None:
            return None
        return deflectors.current_swing(self.status, self.profile, lay, self._york_mode)

    # --------------------------------------------------------------- control

    async def _request(self, **target: Any) -> None:
        await self.coordinator.async_request_change(self._code, **target)

    def _check_mode_change(self, mode: str) -> None:
        if self._locked(LOCK_MODE) and mode != self._york_mode:
            raise _invalid("mode_locked")

    def _mode_target(self, hvac_mode: HVACMode) -> dict[str, Any]:
        if hvac_mode == HVACMode.OFF:
            return {"power": False}
        if hvac_mode not in self.hvac_modes:
            raise _invalid("unsupported_hvac_mode", mode=str(hvac_mode))
        group = _GROUP_BY_HVAC[hvac_mode]
        current = self._york_mode
        if current is not None and MODE_GROUP[current] == group:
            # Already in this group (e.g. floor heating shown as heat): keep it.
            mode = current
        else:
            # The basic mode if the unit has it, else its first mode of the group.
            in_group = [
                m for m in supported_modes(self.status, self.profile) if MODE_GROUP[m] == group
            ]
            mode = group if group in in_group else in_group[0]
        self._check_mode_change(mode)
        return {"power": True, "mode": mode}

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._request(**self._mode_target(hvac_mode))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in (self.preset_modes or []):
            raise _invalid("unsupported_preset_mode", mode=preset_mode)
        self._check_mode_change(preset_mode)
        await self._request(power=True, mode=preset_mode)

    async def async_turn_on(self) -> None:
        await self._request(power=True)

    async def async_turn_off(self) -> None:
        await self._request(power=False)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        target: dict[str, Any] = {}
        mode = self._york_mode
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            target.update(self._mode_target(hvac_mode))
            mode = target.get("mode", mode)
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            if not has_temperature(self.status, mode, self.profile):
                raise _invalid("temperature_not_supported")
            if self._locked(LOCK_TEMP):
                raise _invalid("temperature_locked")
            # Round to the supported step and keep it inside the mode's range.
            step = self.target_temperature_step
            temp = round(float(temp) / step) * step
            low, high = temp_limits(self.status, mode, self.profile)
            target["temperature"] = min(max(temp, low), high)
        if target:
            await self._request(**target)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in (self.fan_modes or []):
            raise _invalid("unsupported_fan_mode", mode=fan_mode)
        if self._locked(LOCK_FAN):
            raise _invalid("fan_locked")
        await self._request(fan=fan_mode)

    async def async_set_humidity(self, humidity: int) -> None:
        """humiChanged (AUTO_WET / WET_FLOOR) or the humidification modal."""
        control = self._humidity
        if control is None:
            raise _invalid("humidity_not_supported")
        low, high = control[1]
        # The sliders have step 1.
        await self._request(humidity=min(max(round(humidity), low), high))

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in (self.swing_modes or []):
            raise _invalid("unsupported_swing_mode", mode=swing_mode)
        await self._request(direction=(1, swing_mode))
