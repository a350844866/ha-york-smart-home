"""Selects for York wire controllers.

* 定时 (simple timer): the control page's picker: "关闭定时", then 0.5 .. 10 h
  in half hours and 11 .. 24 h in whole hours. A timer always toggles the
  power state the unit has when it is set (a running unit is switched off,
  a stopped one on).
* 人感 (one-direction human sensing, or 风吹人 / 风避人 with the 3D sensor).
* The independent (K3) human sensing's action and time when nobody is there.
* 导风板 2..4 (air deflectors beyond the first, which is the climate entity's
  swing mode).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import deflectors
from .api import as_number
from .const import (
    DOMAIN,
    FUNC_PEOPLE,
    K3_STATES,
    K3_TIMES,
    KEY_TIMER_TIME,
    ONE_DIRECTION_OPTIONS,
    TIMER_OFF,
)
from .control import (
    TIMER_OPTIONS,
    current_mode,
    has_simple_timer,
    k3_settings,
    new_timer_turns_on,
    one_direction_options,
    one_direction_value,
    timer_active,
    timer_fired,
    timer_option,
    timer_turns_on,
)
from .coordinator import YorkConfigEntry
from .entity import YorkEntity, async_add_device_entities
from .profile import func_offered

PARALLEL_UPDATES = 0  # the coordinator serializes commands per controller


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, code: (
            [YorkSimpleTimer(coordinator, code)]
            if has_simple_timer(coordinator.devices[code].view)
            else []
        ),
        wait_for_status=True,
    )

    def controls(coordinator, code: str) -> list[SelectEntity]:
        dev = coordinator.devices[code]
        view = dev.view
        entities: list[SelectEntity] = []
        if one_direction_options(view, dev.profile):
            entities.append(YorkOneDirectionSense(coordinator, code))
        if func_offered(view, dev.profile, FUNC_PEOPLE):
            entities.append(YorkHumanSenseSetting(coordinator, code, "state"))
            entities.append(YorkHumanSenseSetting(coordinator, code, "time"))
        if (lay := deflectors.layout(view, dev.profile)) is not None and not lay.unify_only:
            # Individual control: one select per deflector after the first.
            entities.extend(
                YorkDeflector(coordinator, code, number)
                for number, _ in lay.deflectors
                if number > 1
            )
        return entities

    async_add_device_entities(entry, async_add_entities, controls, wait_for_status=True)


class YorkOneDirectionSense(YorkEntity, SelectEntity):
    """人感: one_direction_human_sense off / 风吹人 / 风避人 / 无人节能.

    With has_people_one (human_sense_type "1") this is the rmSN modal (无人节能
    only without the 3D sensor); on a 3D-sensor controller without it, the
    风吹人 / 风避人 function toggles on the same key.
    """

    _attr_translation_key = "human_sensing"

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "human_sensing")

    @property
    def options(self) -> list[str]:
        return one_direction_options(self.status, self.profile)

    @property
    def current_option(self) -> str | None:
        return ONE_DIRECTION_OPTIONS.get(one_direction_value(self.status))

    async def async_select_option(self, option: str) -> None:
        values = {v: k for k, v in ONE_DIRECTION_OPTIONS.items()}
        if option not in self.options:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_option",
                translation_placeholders={"option": option},
            )
        await self.coordinator.async_request_change(self._code, one_direction=values[option])


class YorkHumanSenseSetting(YorkEntity, SelectEntity):
    """The K3 modal's 无人时 action (继续运行 / 待机 / 停机) or judge time (30..180 min).

    Sent together with the on/off switch's value, like the modal. A setting
    of the human sensing (the modal behind the 人感 switch), so a
    configuration entity.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, code: str, field: str) -> None:
        key = "human_sense_action" if field == "state" else "human_sense_nobody_time"
        super().__init__(coordinator, code, key)
        self._field = field
        self._values = K3_STATES if field == "state" else K3_TIMES
        self._attr_translation_key = key
        self._attr_options = list(self._values.values())

    @property
    def current_option(self) -> str | None:
        _, state, time = k3_settings(self.status)
        value = state if self._field == "state" else time
        return None if value is None else self._values[value]

    async def async_select_option(self, option: str) -> None:
        values = {v: k for k, v in self._values.items()}
        if option not in values:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_option",
                translation_placeholders={"option": option},
            )
        await self.coordinator.async_request_change(self._code, k3={self._field: values[option]})


class YorkDeflector(YorkEntity, SelectEntity):
    """导风板 N (N > 1): auto or a fixed level, like the direction modal's
    individual tab (every deflector is sent each time). On a 3D / far-wind
    unit this is 左右风 (deflector 2), sent on its own."""

    _attr_translation_key = "air_deflector"

    def __init__(self, coordinator, code: str, number: int) -> None:
        super().__init__(coordinator, code, f"air_deflector{number}")
        self._number = number
        lay = self._layout
        if lay is not None and lay.special and number == 2:
            # The app's direction list names it 左右风 (unique_id unchanged).
            self._attr_translation_key = "horizontal_air"
        else:
            self._attr_translation_placeholders = {"number": str(number)}

    @property
    def _layout(self) -> deflectors.Layout | None:
        return deflectors.layout(self.status, self.profile)

    @property
    def options(self) -> list[str]:
        lay = self._layout
        if lay is None or self._number not in dict(lay.deflectors):
            return []
        mode = current_mode(self.status, self.profile)
        return deflectors.options(self.profile, lay, self._number, mode)

    @property
    def current_option(self) -> str | None:
        lay = self._layout
        if lay is None or self._number not in dict(lay.deflectors):
            return None
        mode = current_mode(self.status, self.profile)
        return deflectors.current_option(self.status, self.profile, lay, self._number, mode)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_request_change(self._code, direction=(self._number, option))


class YorkSimpleTimer(YorkEntity, SelectEntity):
    """定时: off, or the duration of the running timer.

    Selecting a duration sets a new timer (replacing a running one, as the
    app's picker does); "off" cancels it.
    """

    _attr_translation_key = "simple_timer"
    _attr_options = [TIMER_OFF, *TIMER_OPTIONS]

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "simple_timer")

    @property
    def current_option(self) -> str | None:
        """Off without a timer; else its duration, if known.

        The status only has the end time (the reported simple_timer_time is
        never read by the app, and whether it holds the duration or counts
        down is unknown). The duration is the one set from HA, while the
        cloud still reports that timer (the same timerSimpleTime, or still
        none while the answer had none); a timer set elsewhere, or one whose
        end has passed, shows as unknown (the end is in the timer end sensor).
        """
        status = self.status
        if not timer_active(status):
            return TIMER_OFF
        if self._fired:
            return None
        if (choice := self.device.timer_choice) is not None:
            end, hours = choice
            reported = as_number(status.get(KEY_TIMER_TIME))
            # A (None, hours) choice is pinned to the first reported end
            # after the grace period (coordinator._merge_status).
            same = end is None if reported is None else end == int(reported)
            if same:
                return timer_option(hours)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """action: what the running timer does; next_action: what a new one would do."""
        status = self.status
        attrs = {"next_action": "on" if new_timer_turns_on(status) else "off"}
        if timer_active(status) and not self._fired:
            attrs["action"] = "on" if timer_turns_on(status) else "off"
        return attrs

    @property
    def _fired(self) -> bool:
        return timer_fired(self.status, dt_util.utcnow().timestamp() * 1000)

    async def async_select_option(self, option: str) -> None:
        if option == TIMER_OFF:
            hours = None
        elif (hours := TIMER_OPTIONS.get(option)) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_timer",
                translation_placeholders={"option": option},
            )
        await self.coordinator.async_set_simple_timer(self._code, hours)
