"""Switches for York wire controllers.

* Extra functions (附加功能: 节能, 睡眠, 静音, ...): one switch per function the
  control page offers. A change goes through the page's funcsChanged rules
  (control.build_command): turning one on deselects the functions the app
  deselects, with its side effects, and every offered flag is sent.
* Independent (K3) human sensing (人感): its modal's on/off.
* Settings page toggles (key lock, key sound, auto brightness): the
  settings page's save() always sends its whole bundle (six keys on the
  colour controller, four on the standard one), so each switch sends that
  bundle with only its own value changed (control.build_settings /
  build_standard_settings).
* Cloud scenes (智能场景) with an on/off toggle in the app (sleep curve,
  timers, vacation, linkage scenes ...): the cloud runs them while on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import is_on
from .const import (
    BRIGHTNESS_MODE_KEYS,
    FUNC_PEOPLE,
    KEY_BACKLIGHT_MODE,
    KEY_KEY_LOCK,
    KEY_KEY_SOUND,
    KEY_POWER,
)
from .control import (
    auto_brightness,
    build_settings,
    build_standard_settings,
    current_mode,
    has_color_settings,
    has_standard_settings,
    k3_settings,
)
from .coordinator import YorkConfigEntry
from .entity import (
    YorkEntity,
    YorkSceneEntity,
    async_add_device_entities,
    async_add_scene_entities,
)
from .profile import SWITCH_FUNCS, func_active, func_enabled, func_offered
from .scenes import SWITCH, scene_is_on, scene_status

PARALLEL_UPDATES = 0  # the coordinator serializes commands per controller


@dataclass(frozen=True, kw_only=True)
class YorkSwitchDescription(SwitchEntityDescription):
    """A settings-page toggle."""

    # Keys the controller must report for the switch to be created.
    required: tuple[str, ...]
    value: Callable[[dict[str, Any]], bool]
    # build_settings / build_standard_settings keyword for this toggle.
    setting: str


_KEY_LOCK = YorkSwitchDescription(
    key=KEY_KEY_LOCK,  # 按键操作锁定
    translation_key="key_lock",
    entity_category=EntityCategory.CONFIG,
    required=(KEY_KEY_LOCK,),
    value=lambda status: is_on(status.get(KEY_KEY_LOCK)),
    setting="key_lock",
)
_KEY_SOUND = YorkSwitchDescription(
    key=KEY_KEY_SOUND,  # 触控音
    translation_key="key_sound",
    entity_category=EntityCategory.CONFIG,
    required=(KEY_KEY_SOUND,),
    value=lambda status: is_on(status.get(KEY_KEY_SOUND)),
    setting="key_sound",
)
SWITCHES: tuple[YorkSwitchDescription, ...] = (
    _KEY_LOCK,
    _KEY_SOUND,
    YorkSwitchDescription(
        key="auto_brightness",  # 亮度调节 → 自动
        translation_key="auto_brightness",
        entity_category=EntityCategory.CONFIG,
        required=BRIGHTNESS_MODE_KEYS,
        value=auto_brightness,
        setting="auto",
    ),
)
# The standard page (xkq-set): "自动" is the seventh brightness level and
# only backlight_brightness_mode exists.
STANDARD_SWITCHES: tuple[YorkSwitchDescription, ...] = (
    _KEY_LOCK,
    _KEY_SOUND,
    YorkSwitchDescription(
        key="auto_brightness",
        translation_key="auto_brightness",
        entity_category=EntityCategory.CONFIG,
        required=(KEY_BACKLIGHT_MODE,),
        value=lambda status: auto_brightness(status, standard=True),
        setting="auto",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def create(coordinator, code: str) -> list[SwitchEntity]:
        dev = coordinator.devices[code]
        view = dev.view
        entities: list[SwitchEntity] = [
            YorkFunctionSwitch(coordinator, code, func)
            for func in SWITCH_FUNCS
            if func_offered(view, dev.profile, func)
        ]
        if func_offered(view, dev.profile, FUNC_PEOPLE):
            entities.append(YorkHumanSenseSwitch(coordinator, code))
        if has_color_settings(dev.xkq_type):
            settings = SWITCHES
        elif has_standard_settings(dev.xkq_type):
            settings = STANDARD_SWITCHES
        else:
            settings = ()
        entities.extend(
            YorkSettingSwitch(coordinator, code, description)
            for description in settings
            if all(view.get(key) is not None for key in description.required)
        )
        return entities

    async_add_device_entities(entry, async_add_entities, create, wait_for_status=True)
    async_add_scene_entities(
        entry,
        async_add_entities,
        lambda coordinator, scene: (
            [YorkSceneSwitch(coordinator, scene)]
            if scene.stype is not None and scene.stype.control == SWITCH
            else []
        ),
    )


class YorkSceneSwitch(YorkSceneEntity, SwitchEntity):
    """A cloud scene the app switches on and off; the cloud runs it while on.

    On as the app's toggle shows it, with a hung-up linkage scene as on
    (scenes.scene_is_on); attribute ``status`` is the raw state, including
    "hangup" (HANGUP, "2").
    """

    _scene_control = SWITCH

    @property
    def is_on(self) -> bool | None:
        if (scene := self.scene) is None or (stype := self.stype) is None:
            return None
        return scene_is_on(scene.record, stype)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attributes = super().extra_state_attributes
        if attributes is None or (scene := self.scene) is None or (stype := self.stype) is None:
            return attributes
        return {**attributes, "status": scene_status(scene.record, stype)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_scene(self._home_id, self._smart_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_scene(self._home_id, self._smart_id, False)


class YorkFunctionSwitch(YorkEntity, SwitchEntity):
    """One extra function of the control page's 功能 list.

    The app only lets a function be toggled when refreshFuncEnable allows
    it (power, mode, locks); otherwise HA refuses the change and the switch
    stays available so its state remains visible (attribute ``changeable``).
    Self clean and high-temperature sterilization can only be started with
    the unit off (they switch it on); turning them off is the overlay's
    stop button, which also switches the unit off.
    """

    def __init__(self, coordinator, code: str, func: str) -> None:
        super().__init__(coordinator, code, func)
        self._func = func
        self._attr_translation_key = func

    @property
    def is_on(self) -> bool:
        return func_active(self.status, self._func)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.status
        on = is_on(status.get(KEY_POWER))
        mode = current_mode(status, self.profile)
        return {"changeable": func_enabled(status, self.profile, self._func, on=on, mode=mode)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_request_change(self._code, funcs={self._func: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_request_change(self._code, funcs={self._func: False})


class YorkHumanSenseSwitch(YorkEntity, SwitchEntity):
    """人感 (independent / K3 human sensing) on or off.

    The modal always sends independent_human_sense_effect, _state and
    _nobody_judge_time together (see the two selects for the others).
    """

    _attr_translation_key = "independent_human_sense"

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "independent_human_sense")

    @property
    def is_on(self) -> bool:
        return k3_settings(self.status)[0] == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_request_change(self._code, k3={"effect": "1"})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_request_change(self._code, k3={"effect": "0"})


class YorkSettingSwitch(YorkEntity, SwitchEntity):
    """One toggle of the settings page."""

    entity_description: YorkSwitchDescription

    def __init__(self, coordinator, code: str, description: YorkSwitchDescription) -> None:
        super().__init__(coordinator, code, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.value(self.status)

    async def _set(self, on: bool) -> None:
        description = self.entity_description
        standard = has_standard_settings(self.device.xkq_type)
        builder = build_standard_settings if standard else build_settings

        def build(view: dict[str, Any]) -> dict[str, Any]:
            if description.value(view) == on:
                return {}  # already so: nothing is sent
            return builder(view, **{description.setting: on})

        # fresh: the bundle resends every setting, so build it from the
        # controller's current state like the settings page does.
        await self.coordinator.async_send_raw(self._code, build, fresh=True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
