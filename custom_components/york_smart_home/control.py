"""Build wire-controller commands exactly like the app's control pages.

Mirrors the control page (xkq-color-control, or xkq-control for the
standard controller; see profile.py for their differences): ``control()``
and the handlers feeding it (switchChanged, modeChanged, tempChanged,
humiChanged, windChanged, funcsChanged, the human-sensing and humidification
modals, directionChanged). Only fields that differ from the current state
are sent, and a change also switches off the extra functions the app
switches off, with the app's side effects. No Home Assistant imports, so it
can be unit tested.

``status`` here is the device's merged view: live status with the
controller's constants (capabilities, locks, limits) laid over it.
"""

from __future__ import annotations

from typing import Any

from . import deflectors
from .api import XKQ_TYPE_COLOR, XKQ_TYPE_STANDARD, as_number, is_on
from .const import (
    BRIGHTNESS_MODE_KEYS,
    FAN_MODES,
    FILTER_TIPS_KEYS,
    FUNC_CLEAN,
    FUNC_HEALTH,
    FUNC_HUMAN_SENSING,
    FUNC_HUMIDIFY,
    FUNC_LEAVE,
    FUNC_NO_WIND,
    FUNC_PEOPLE,
    FUNC_PEOPLE_ONE,
    FUNC_SLEEP,
    FUNC_SMART_PEOPLE,
    FUNC_STERILIZE,
    FUNC_WIND_BLOW,
    FUNC_WIND_SHELTER,
    HUMIDIFICATION_LIMIT,
    HUMIDITY_LIMIT,
    HUMIDITY_LIMIT_DRY,
    K3_STATES,
    K3_TIMES,
    KEY_BACKLIGHT_BRIGHTNESS,
    KEY_BACKLIGHT_MODE,
    KEY_FILTER_RESET,
    KEY_INDICATOR_BRIGHTNESS,
    KEY_INDICATOR_MODE,
    KEY_K3_EFFECT,
    KEY_K3_STATE,
    KEY_K3_TIME,
    KEY_KEY_LOCK,
    KEY_KEY_SOUND,
    KEY_ONE_DIRECTION_HUMAN_SENSE,
    KEY_POWER,
    KEY_SET_HUMIDITY,
    KEY_SET_TEMP,
    KEY_SET_TEMP_HALF,
    KEY_TIMER_FLAG,
    KEY_TIMER_ONOFF,
    KEY_TIMER_TIME,
    MODE_AUTO,
    MODE_FLOOR_HEAT,
    MODES,
    MODES_COOL_RANGE,
    MODES_WITHOUT_FAN,
    MODES_WITHOUT_TEMPERATURE,
    ONE_DIRECTION_OPTIONS,
    STANDARD_BRIGHTNESS_LEVELS,
    TEMP_LIMIT_AUTO,
    TEMP_LIMIT_COOL,
    TEMP_LIMIT_HEAT,
    TEMP_LIMIT_NONE,
    TIMER_HOURS,
    TIMER_KEYS,
)
from .profile import (
    COLOR,
    DRY_HUMIDITY_MODES,
    FUNC_KEYS,
    HUMAN_FUNCS,
    HUMIDITY_MODES,
    INT_FUNCS,
    Profile,
    active_funcs,
    fresh_air,
    func_active,
    func_enabled,
    func_offered,
    model_str,
    offered_funcs,
)

# Human sensing turned on (oneHumanFeelChanged / humanFeelChanged) deselects
# only these (the funcsChanged cases for PEOPLE / PEOPLE_ONE are dead code).
_CANCEL_ON_HUMAN_SENSE = frozenset({FUNC_CLEAN, FUNC_STERILIZE, FUNC_NO_WIND})


class CommandRefused(Exception):
    """The app does not allow this change in the current state.

    ``key`` is the translation key of the user-facing error. A ``function``
    placeholder holds the function id (FUNC_*); the coordinator replaces it
    with the function's name (const.FUNC_LABELS) before showing the error.
    Option placeholders use the select's option ids where there is one.
    """

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


# ------------------------------------------------------------ modes, limits


def supported_modes(status: dict[str, Any], profile: Profile = COLOR) -> list[str]:
    """getModeArr: modes whose support key is "1", in the app's order."""
    return [m for m in profile.modes if is_on(status.get(MODES[m][1]))]


def current_mode(status: dict[str, Any], profile: Profile = COLOR) -> str | None:
    """getMode: the first mode (app order) whose control key is "1"."""
    return next((m for m in profile.modes if is_on(status.get(MODES[m][0]))), None)


def has_temperature(status: dict[str, Any], mode: str | None, profile: Profile = COLOR) -> bool:
    """refreshTempEnable: whether the app shows the setpoint in ``mode``.

    A fresh-air colour controller (xkqColorType WIND) never shows it; the
    standard page shows humidity instead of temperature in the same modes.
    """
    return not fresh_air(status, profile) and mode not in MODES_WITHOUT_TEMPERATURE


def has_fan(mode: str | None) -> bool:
    """refreshWindDisplay: the fan speed is hidden in floor heating."""
    return mode not in MODES_WITHOUT_FAN


def supported_fans(status: dict[str, Any], profile: Profile = COLOR) -> list[str]:
    """getWindArr: speeds whose support key is "1", lowest first.

    The colour page empties the list when "auto" is the only speed.
    """
    fans = [f for f, (_, flag) in FAN_MODES.items() if is_on(status.get(flag))]
    return [] if profile.hide_auto_only_fan and fans == ["auto"] else fans


def current_fan(status: dict[str, Any]) -> str | None:
    return next((f for f, (key, _) in FAN_MODES.items() if is_on(status.get(key))), None)


def current_temperature_setpoint(status: dict[str, Any]) -> float | None:
    temp = as_number(status.get(KEY_SET_TEMP))
    if temp is None or not 5 <= temp <= 40:
        return None
    return temp + (0.5 if is_on(status.get(KEY_SET_TEMP_HALF)) else 0)


def temp_limits(
    status: dict[str, Any], mode: str | None, profile: Profile = COLOR
) -> tuple[float, float]:
    """getTempLimit: cooling or heating range by mode, 16-32 for auto.

    Modes without a case in the service (fresh-air eco; purification on the
    standard page) fall back to 1-99, i.e. no limit. An unknown mode uses
    the cooling range.
    """
    if mode == MODE_AUTO:
        return TEMP_LIMIT_AUTO
    if mode is not None and mode not in MODES_COOL_RANGE and mode not in profile.heat_range:
        return TEMP_LIMIT_NONE
    heat = mode in profile.heat_range
    low_key, high_key = TEMP_LIMIT_HEAT if heat else TEMP_LIMIT_COOL
    default = (16, 32) if heat else (19, 30)
    low = as_number(status.get(low_key))
    high = as_number(status.get(high_key))
    if low is None or high is None or not 5 <= low < high <= 40:
        return default
    return low, high


def encode_temperature(temp: float) -> dict[str, Any]:
    """Whole degrees in set_temp, the half degree as a separate flag."""
    whole = int(temp) if temp == int(temp) else int(temp - 0.5)
    return {KEY_SET_TEMP: whole, KEY_SET_TEMP_HALF: "1" if temp != whole else "0"}


# ----------------------------------------------------------------- humidity


def humidity_limits(mode: str | None) -> tuple[int, int]:
    """getHumiLimit: 35-90 in the dehumidifying modes, else 35-70."""
    return HUMIDITY_LIMIT_DRY if mode in DRY_HUMIDITY_MODES else HUMIDITY_LIMIT


def current_humidity_setpoint(status: dict[str, Any]) -> int | None:
    """set_humidity (the page's humi), or None if missing or not 1..100."""
    value = as_number(status.get(KEY_SET_HUMIDITY))
    if value is None or not 0 < value <= 100:
        return None
    return int(value)


def humidity_control(
    status: dict[str, Any], mode: str | None, profile: Profile = COLOR
) -> tuple[str, tuple[int, int]] | None:
    """Where the app sets the target humidity now, and its limits.

    ("mode", getHumiLimit) when the page shows its humidity slider instead
    of the temperature (AUTO_WET, WET_FLOOR; never on a fresh-air
    controller); ("humidify", 30-60) while the humidification function is
    on (its modal's slider); else None.
    """
    if mode in HUMIDITY_MODES and not fresh_air(status, profile):
        return "mode", humidity_limits(mode)
    if func_offered(status, profile, FUNC_HUMIDIFY) and func_active(status, FUNC_HUMIDIFY):
        return "humidify", HUMIDIFICATION_LIMIT
    return None


# ------------------------------------------------------------ human sensing


def one_direction_options(status: dict[str, Any], profile: Profile = COLOR) -> list[str]:
    """Options of the one-direction human sensing ("0".."3" as option ids).

    With has_people_one the rmSN modal offers 风吹人 / 风避人 and, without the
    3D sensor, 无人节能; a 3D-sensor controller without it has only the
    风吹人 / 风避人 toggles (WIND_BLOW_HUM / WIND_SHELTER_HUM). Empty when
    neither applies.
    """
    if func_offered(status, profile, FUNC_PEOPLE_ONE):
        values = ["0", "1", "2"]
        if model_str(status.get("human_sense_type2")) != "2":
            values.append("3")
    elif func_offered(status, profile, FUNC_WIND_BLOW):
        values = ["0", "1", "2"]
    else:
        return []
    return [ONE_DIRECTION_OPTIONS[v] for v in values]


def one_direction_value(status: dict[str, Any]) -> str:
    """one_direction_human_sense as the page's model holds it ("0" if unset)."""
    return model_str(status.get(KEY_ONE_DIRECTION_HUMAN_SENSE))


def k3_settings(status: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """getPeople: (effect, state, nobody time); unknown values are None."""
    state = model_str(status.get(KEY_K3_STATE))
    time = model_str(status.get(KEY_K3_TIME))
    return (
        "1" if model_str(status.get(KEY_K3_EFFECT)) == "1" else "0",
        state if state in K3_STATES else None,
        time if time in K3_TIMES else None,
    )


# ------------------------------------------------------------- the page


class _Page:
    """The control page's model for one change, and its control() diff."""

    def __init__(self, status: dict[str, Any], profile: Profile) -> None:
        self.status = status
        self.profile = profile
        self.offered = offered_funcs(status, profile)
        self.on = is_on(status.get(KEY_POWER))
        self.mode = current_mode(status, profile)
        self.temp = current_temperature_setpoint(status)
        self.humi = current_humidity_setpoint(status)
        self.fan = current_fan(status)
        self.funcs = active_funcs(status, profile)
        self.people_one = one_direction_value(status)
        effect, state, time = k3_settings(status)
        self.people = (effect, state, time)
        self.layout = deflectors.layout(status, profile)
        self.direction: dict[str, str] = {}
        self._pre = (
            self.on,
            self.mode,
            self.temp,
            self.humi,
            self.fan,
            set(self.funcs),
            self.people_one,
            self.people,
        )

    # ----------------------------------------------------------- handlers

    def enabled(self, func: str) -> bool:
        return func_enabled(self.status, self.profile, func, on=self.on, mode=self.mode)

    def _refuse_unless_offered(self, func: str) -> None:
        if func not in self.offered:
            raise CommandRefused("function_not_supported", function=func)

    def _refuse_unless_enabled(self, func: str) -> None:
        """The page only lets enabled functions be toggled (changeSwitch)."""
        self._refuse_unless_offered(func)
        if not self.enabled(func):
            raise CommandRefused("function_not_available", function=func)

    def close_one_direction(self) -> None:
        """closeOneHumanFeeling: peopleOne "0" and PEOPLE_ONE deselected."""
        self.people_one = "0"
        self.funcs.discard(FUNC_PEOPLE_ONE)

    def close_far(self) -> None:
        """closeFarDirection: selecting 上下风 again while 超远风 is selected."""
        self.direction.update(deflectors.close_far(self.status, self.layout))

    def switch(self, on: bool) -> None:
        """switchChanged."""
        if on == self.on:
            return
        self.on = on
        if not on:
            # Colour: everything off; standard: a running self clean stays.
            self.funcs &= self.profile.kept_on_power_off
        if self.profile.closes_one_direction:
            self.close_one_direction()

    def set_mode(self, mode: str) -> None:
        """modeChanged: clamp setpoints, keep only HEALTH (not in floor heating)."""
        if mode == self.mode:
            return
        self.mode = mode
        if self.temp is not None:
            low, high = temp_limits(self.status, mode, self.profile)
            self.temp = min(max(self.temp, low), high)
        if self.humi is not None:
            low, high = humidity_limits(mode)
            self.humi = min(max(self.humi, low), high)
        keep = FUNC_HEALTH in self.funcs and mode != MODE_FLOOR_HEAT
        self.funcs = {FUNC_HEALTH} if keep else set()
        if self.profile.closes_one_direction:
            self.close_one_direction()

    def set_temp(self, temp: float) -> None:
        """tempChanged, within the slider's min/max for the (new) mode."""
        low, high = temp_limits(self.status, self.mode, self.profile)
        temp = min(max(temp, low), high)
        if temp != self.temp:
            self.temp = temp
            self.funcs -= self.profile.cancel_on_temp

    def set_humidity(self, humi: int) -> None:
        """humiChanged (main page slider), or the humidification modal."""
        control = humidity_control(self.status, self.mode, self.profile)
        if control is None:
            raise CommandRefused("humidity_not_supported")
        if humi == self.humi:
            return
        if control[0] == "humidify":
            # The modal only opens while HUMI can be toggled; no cancellation.
            self._refuse_unless_enabled(FUNC_HUMIDIFY)
            self.humi = humi
        else:
            self.humi = humi
            self.funcs -= self.profile.cancel_on_temp

    def set_fan(self, fan: str) -> None:
        """windChanged."""
        if fan != self.fan:
            self.fan = fan
            self.funcs -= self.profile.cancel_on_fan

    def toggle(self, func: str, on: bool) -> None:
        """funcsChanged (and the humidification / smart sensing dialogs)."""
        self._refuse_unless_offered(func)
        if on == (func in self.funcs):
            return  # already so: nothing to toggle
        self._refuse_unless_enabled(func)
        if func == FUNC_HUMIDIFY:
            # humidificationChanged: no exclusions, no other side effects.
            if on:
                self.funcs.add(func)
            else:
                self.funcs.discard(func)
            return
        if on:
            if func in (FUNC_WIND_BLOW, FUNC_WIND_SHELTER, FUNC_SMART_PEOPLE):
                self.close_far()
            self.funcs -= self.profile.exclusions.get(func, frozenset())
            self.funcs.add(func)
        else:
            self.funcs.discard(func)
        self._after_funcs_changed()

    def _after_funcs_changed(self) -> None:
        """The side effects at the end of funcsChanged."""
        if FUNC_SLEEP in self.funcs:
            # 3 or 4 speeds: low; 6 or 7: breeze (微风). The app does not
            # check the speed exists (it would fail to send); HA skips it.
            fans = supported_fans(self.status, self.profile)
            target = {3: "low", 4: "low", 6: "silent", 7: "silent"}.get(len(fans))
            if target in fans:
                self.fan = target
        if FUNC_CLEAN in self.funcs or FUNC_STERILIZE in self.funcs:
            self.on = True
        if FUNC_LEAVE in self.funcs:
            heat_down = as_number(self.status.get("heat_down_value"))
            self.temp = heat_down if heat_down is not None and heat_down > 19 else 19
        # The app resets these whenever the function is not selected, even
        # on controllers that do not offer it (then it sends
        # independent_human_sense_* or one_direction_human_sense "0" with
        # any function change); HA only does it where the function exists.
        if FUNC_PEOPLE in self.offered and FUNC_PEOPLE not in self.funcs:
            self.people = ("0", self.people[1], self.people[2])
        if FUNC_PEOPLE_ONE in self.offered and FUNC_PEOPLE_ONE not in self.funcs:
            self.people_one = "0"

    def set_one_direction(self, value: str) -> None:
        """The one-direction human sensing modal, or 风吹人 / 风避人."""
        if FUNC_PEOPLE_ONE in self.offered:
            if value not in ("0", "1", "2", "3") or (
                value == "3" and model_str(self.status.get("human_sense_type2")) == "2"
            ):
                raise CommandRefused(
                    "unsupported_option", option=ONE_DIRECTION_OPTIONS.get(value, value)
                )
            if value == self.people_one:
                return
            self._refuse_unless_enabled(FUNC_PEOPLE_ONE)
            self.people_one = value
            if value != "0":
                self.close_far()
                # oneHumanFeelChanged
                self.funcs.add(FUNC_PEOPLE_ONE)
                self.funcs -= _CANCEL_ON_HUMAN_SENSE
            else:
                self.funcs.discard(FUNC_PEOPLE_ONE)
            return
        if FUNC_WIND_BLOW not in self.offered:
            raise CommandRefused("function_not_supported", function=FUNC_HUMAN_SENSING)
        if value not in ("0", "1", "2"):
            raise CommandRefused(
                "unsupported_option", option=ONE_DIRECTION_OPTIONS.get(value, value)
            )
        if value == "1":
            self.toggle(FUNC_WIND_BLOW, True)
        elif value == "2":
            self.toggle(FUNC_WIND_SHELTER, True)
        else:
            for func in (FUNC_WIND_BLOW, FUNC_WIND_SHELTER):
                if func in self.funcs:
                    self.toggle(func, False)

    def set_k3(self, effect: str | None, state: str | None, time: str | None) -> None:
        """The independent (K3) human sensing modal: humanFeelChanged."""
        self._refuse_unless_offered(FUNC_PEOPLE)
        cur_effect, cur_state, cur_time = self.people
        # The modal needs valid values; start from its first option.
        people = (
            cur_effect if effect is None else effect,
            (cur_state or "1") if state is None else state,
            (cur_time or "0") if time is None else time,
        )
        if people[0] not in ("0", "1") or people[1] not in K3_STATES or people[2] not in K3_TIMES:
            raise CommandRefused("unsupported_option", option=",".join(people))
        if people == self.people:
            return
        self._refuse_unless_enabled(FUNC_PEOPLE)
        self.people = people
        if self.people[0] == "1":
            self.funcs.add(FUNC_PEOPLE)
            self.funcs -= _CANCEL_ON_HUMAN_SENSE
        else:
            self.funcs.discard(FUNC_PEOPLE)

    def set_direction(self, number: int, option: str) -> None:
        """directionChanged: cancel functions, close one-direction sensing, send."""
        lay = self.layout
        if lay is None or number not in dict(lay.deflectors):
            raise CommandRefused("swing_not_supported")
        allowed = (
            deflectors.swing_options(self.profile, lay, self.mode)
            if number == 1
            else deflectors.options(self.profile, lay, number, self.mode)
        )
        if option not in allowed:
            raise CommandRefused("unsupported_swing_mode", mode=option)
        if not deflectors.direction_enabled(self.on, self.mode):
            raise CommandRefused("swing_not_available")
        if self.profile.closes_one_direction:
            self.close_one_direction()
        self.funcs -= self.profile.cancel_on_direction
        self.direction.update(
            deflectors.command(self.status, self.profile, lay, self.mode, number, option)
        )

    # -------------------------------------------------------------- control

    def command(self) -> dict[str, Any]:
        """control(): the fields that differ from the state the page loaded."""
        on, mode, temp, humi, fan, funcs, people_one, people = self._pre
        cmd: dict[str, Any] = {}
        if self.on != on:
            cmd[KEY_POWER] = "1" if self.on else "0"
        if self.mode != mode and self.mode is not None:
            cmd[MODES[self.mode][0]] = "1"
        if self.temp != temp and self.temp is not None:
            cmd.update(encode_temperature(self.temp))
        if self.humi != humi and self.humi is not None:
            cmd[KEY_SET_HUMIDITY] = int(self.humi)
        if self.fan != fan and self.fan is not None:
            cmd[FAN_MODES[self.fan][0]] = "1"
        if self.funcs != funcs:
            # Every offered function's flag once the set changes.
            for func in self.offered:
                if func in HUMAN_FUNCS and not (
                    func == FUNC_PEOPLE and self.profile.people_in_flags
                ):
                    continue
                cmd[FUNC_KEYS[func][0]] = self._flag(func, func in self.funcs)
        if self.profile.closes_one_direction:
            if self.people_one != people_one:
                cmd[KEY_ONE_DIRECTION_HUMAN_SENSE] = self.people_one
            blow, shelter = FUNC_WIND_BLOW in self.funcs, FUNC_WIND_SHELTER in self.funcs
            if blow != (FUNC_WIND_BLOW in funcs) or shelter != (FUNC_WIND_SHELTER in funcs):
                cmd[KEY_ONE_DIRECTION_HUMAN_SENSE] = "1" if blow else "2" if shelter else "0"
        if self.people != people:
            effect, state, time = self.people
            cmd[KEY_K3_EFFECT] = effect
            cmd[KEY_K3_STATE] = state or "1"
            cmd[KEY_K3_TIME] = time or "0"
        # controlDirection is a separate request in the app, sent right away
        # (control() waits 1.5 s); here both go in one ctrlJson.
        cmd.update(self.direction)
        return cmd

    def _flag(self, func: str, on: bool) -> Any:
        if func in INT_FUNCS:
            if on:
                return 1
            return self.profile.sterilize_off if func == FUNC_STERILIZE else 0
        return "1" if on else "0"


def build_command(
    status: dict[str, Any], target: dict[str, Any], profile: Profile = COLOR
) -> dict[str, Any]:
    """ctrlJson turning ``status`` into ``target``; empty if nothing changes.

    ``target`` may hold ``power`` (bool), ``mode``, ``temperature``,
    ``humidity``, ``fan``, ``funcs`` ({id: bool}), ``one_direction`` ("0".."3"),
    ``k3`` ({"effect"/"state"/"time": str}) and ``direction`` ((deflector
    number, option)); missing keys keep their current value. The handlers
    run in the page's order (power, mode, temperature, humidity, fan,
    functions, human sensing, direction). Raises CommandRefused for changes
    the page does not allow in the resulting state.

    Stopping a running self clean or sterilization is the overlay's stop
    button: {self_clean: 0, if_run: "0"} (resp. hig_temperature_sterilization),
    not a function change.
    """
    funcs = target.get("funcs") or {}
    for func in (FUNC_CLEAN, FUNC_STERILIZE):
        if funcs.get(func) is False:
            if not func_active(status, func):
                return {}
            return {FUNC_KEYS[func][0]: 0, KEY_POWER: "0"}

    page = _Page(status, profile)
    if "power" in target:
        page.switch(bool(target["power"]))
    if target.get("mode") is not None:
        page.set_mode(target["mode"])
    if target.get("temperature") is not None:
        page.set_temp(target["temperature"])
    if target.get("humidity") is not None:
        page.set_humidity(int(target["humidity"]))
    if target.get("fan") is not None:
        page.set_fan(target["fan"])
    for func, on in funcs.items():
        page.toggle(func, bool(on))
    if target.get("one_direction") is not None:
        page.set_one_direction(str(target["one_direction"]))
    if (k3 := target.get("k3")) is not None:
        page.set_k3(k3.get("effect"), k3.get("state"), k3.get("time"))
    if (direction := target.get("direction")) is not None:
        page.set_direction(*direction)
    return page.command()


def command_state(
    status: dict[str, Any], cmd: dict[str, Any], profile: Profile = COLOR
) -> dict[str, Any]:
    """Local state after an accepted command: its fields plus what they imply.

    On 3D / far-wind units the other direction entries are deselected
    locally (the app never sends their "0").
    """
    state = dict(cmd)
    state.update(deflectors.implied_state(deflectors.layout(status, profile), cmd))
    return state


def functions_enabled(status: dict[str, Any], profile: Profile = COLOR) -> dict[str, bool]:
    """refreshFuncEnable for every offered function in the current state."""
    on = is_on(status.get(KEY_POWER))
    mode = current_mode(status, profile)
    return {
        func: func_enabled(status, profile, func, on=on, mode=mode)
        for func in offered_funcs(status, profile)
    }


# ---------------------------------------------------------------------------
# Settings page (xkq-color-set, xkq-set) and filter reset. These do not go
# through build_command: they are not diffed and cancel no extra functions.
# ---------------------------------------------------------------------------


def has_filter_tips(status: dict[str, Any]) -> bool:
    """The controller reports at least one filter_life_tips slot."""
    return any(status.get(key) is not None for key in FILTER_TIPS_KEYS)


def filter_reset_command() -> dict[str, Any]:
    """filterScreenReset: a fresh (empty) control model with only this flag."""
    return {KEY_FILTER_RESET: "1"}


def _tip_due(key: str, value: float | None, profile: Profile) -> bool:
    """allowShowLW for one slot: 2, or 3 on slot 3 (colour page); the
    standard page only looks at filter_life_tips1 == 2."""
    if not profile.filter_all_slots:
        return key == FILTER_TIPS_KEYS[0] and value == 2
    return value == 2 or (value == 3 and key == FILTER_TIPS_KEYS[2])


def filter_due(status: dict[str, Any], profile: Profile = COLOR) -> bool | None:
    """The page's filter banner (allowShowLW); None without usable slots.

    A slot the page reads that is not a number makes it unknown.
    """
    keys = FILTER_TIPS_KEYS if profile.filter_all_slots else FILTER_TIPS_KEYS[:1]
    due = False
    seen = False
    for key in keys:
        raw = status.get(key)
        if raw is None:
            continue
        if (value := as_number(raw)) is None:
            return None
        seen = True
        due = due or _tip_due(key, value, profile)
    return due if seen else None


# The fresh-air controller's filter dialog (Gh+9): slot -> (name, what the
# value means). 2 is 需清洁 for the primary filter and the activated carbon
# (3: 需更换), 需更换 for the others.
FRESH_AIR_FILTERS: dict[str, tuple[str, dict[int, str]]] = {
    "filter_life_tips1": ("primary", {2: "clean"}),
    "filter_life_tips2": ("humidification", {2: "replace"}),
    "filter_life_tips3": ("activated_carbon", {2: "clean", 3: "replace"}),
    "filter_life_tips4": ("fresh_air_pm25", {2: "replace"}),
    "filter_life_tips5": ("return_air_pm25", {2: "replace"}),
}


def fresh_air_filters(status: dict[str, Any]) -> dict[str, str]:
    """Each reported slot's state for the fresh-air filter dialog: ok/clean/replace."""
    result = {}
    for key, (name, states) in FRESH_AIR_FILTERS.items():
        value = as_number(status.get(key))
        if value is not None:
            result[name] = states.get(int(value), "ok")
    return result


def filter_reset_state(status: dict[str, Any], profile: Profile = COLOR) -> dict[str, Any]:
    """Local state after a successful reset: the reminder is cleared.

    The app just hides its filter banner (allowShowLW = false) until the
    next report; here every slot that raises the reminder is set to 0,
    keeping the slot's type.
    """
    cleared: dict[str, Any] = {}
    for key in FILTER_TIPS_KEYS:
        if _tip_due(key, as_number(status.get(key)), profile):
            cleared[key] = "0" if isinstance(status[key], str) else 0
    return cleared


def has_color_settings(xkq_type: str) -> bool:
    """Whether the colour controller's settings page (xkq-color-set) applies.

    The colour controller (xkqType "9") uses the six-key bundle with
    1..100 brightness; the standard controller (xkqType "8") has its own
    page (see build_standard_settings).
    """
    return xkq_type == XKQ_TYPE_COLOR


def has_standard_settings(xkq_type: str) -> bool:
    """Whether the standard controller's settings page (xkq-set) applies."""
    return xkq_type == XKQ_TYPE_STANDARD


def auto_brightness(status: dict[str, Any], standard: bool = False) -> bool:
    """The settings page's "自动" state.

    Colour page ngOnInit: auto unless both mode keys are "0"
    (``"0" !== indicator_light_mode || "0" !== backlight_brightness_mode``).
    Standard page: lightIndex 6 (自动) unless backlight_brightness_mode is
    "0". A missing key therefore also reads as auto. HA also accepts a
    numeric 0 as "0", which the app's strict comparison would not.
    """
    keys = (KEY_BACKLIGHT_MODE,) if standard else BRIGHTNESS_MODE_KEYS
    return any(str(status.get(key)) != "0" for key in keys)


def build_settings(
    status: dict[str, Any],
    *,
    key_lock: bool | None = None,
    key_sound: bool | None = None,
    auto: bool | None = None,
    backlight: int | None = None,
    indicator: int | None = None,
) -> dict[str, Any]:
    """The colour settings page's save() ctrlJson with one or more values changed.

    save() always sends all six keys, built from the page's model, which it
    loaded from a full status query; this does the same from the current
    view and overrides only what is given. Lock, sound and the two mode keys
    are "1"/"0" strings (both mode keys follow the single auto toggle); the
    brightness values are sent as they were reported unless changed, then as
    integers (the slider's number). A brightness the controller does not
    report is left out, as JSON.stringify drops the app's undefined value.
    """
    if key_lock is None:
        key_lock = is_on(status.get(KEY_KEY_LOCK))
    if key_sound is None:
        key_sound = is_on(status.get(KEY_KEY_SOUND))
    if auto is None:
        auto = auto_brightness(status)
    cmd: dict[str, Any] = {
        KEY_KEY_LOCK: "1" if key_lock else "0",
        KEY_KEY_SOUND: "1" if key_sound else "0",
        KEY_INDICATOR_MODE: "1" if auto else "0",
        KEY_BACKLIGHT_MODE: "1" if auto else "0",
    }
    for key, value in (
        (KEY_INDICATOR_BRIGHTNESS, indicator),
        (KEY_BACKLIGHT_BRIGHTNESS, backlight),
    ):
        if value is not None:
            cmd[key] = int(value)
        elif status.get(key) is not None:
            cmd[key] = status[key]
    return cmd


def standard_brightness_level(status: dict[str, Any]) -> int | None:
    """The standard page's brightness level 1..6 (一档..六档); None while auto.

    backlight_brightness holds the level's index 0..5 (lightIndex); a value
    outside it is unknown.
    """
    if auto_brightness(status, standard=True):
        return None
    value = as_number(status.get(KEY_BACKLIGHT_BRIGHTNESS))
    if value is None or not 0 <= value < STANDARD_BRIGHTNESS_LEVELS or value != int(value):
        return None
    return int(value) + 1


def build_standard_settings(
    status: dict[str, Any],
    *,
    key_lock: bool | None = None,
    key_sound: bool | None = None,
    auto: bool | None = None,
    level: int | None = None,
) -> dict[str, Any]:
    """The standard settings page's save() ctrlJson (xkq-set).

    Always four keys, in the page's order: key_lock, backlight_brightness
    (the level's index 0..5, 0 while auto), backlight_brightness_mode ("1"
    for auto) and key_sound_switch. ``level`` is 1..6. Turning auto off
    without a level keeps the reported one (else 一档, index 0).
    """
    if key_lock is None:
        key_lock = is_on(status.get(KEY_KEY_LOCK))
    if key_sound is None:
        key_sound = is_on(status.get(KEY_KEY_SOUND))
    if auto is None:
        auto = auto_brightness(status, standard=True)
    if level is None:
        value = as_number(status.get(KEY_BACKLIGHT_BRIGHTNESS))
        valid = value is not None and value == int(value) and 0 <= value < 6
        index = int(value) if valid else 0
    else:
        index = int(level) - 1
    return {
        KEY_KEY_LOCK: "1" if key_lock else "0",
        KEY_BACKLIGHT_BRIGHTNESS: 0 if auto else index,
        KEY_BACKLIGHT_MODE: "1" if auto else "0",
        KEY_KEY_SOUND: "1" if key_sound else "0",
    }


# ---------------------------------------------------------------------------
# Simple timer (简易定时). Sent to its own endpoint (YorkApi.set_simple_timer),
# not through multiIuOuCtrl; these helpers read and predict its state.
# ---------------------------------------------------------------------------

# Timer ends further than this from now are treated as implausible (the
# longest timer is 24 h; the margin covers clock differences).
_TIMER_END_WINDOW_MS = 25 * 3600 * 1000
# An end further than this in the past has fired. The app would still show
# it (getTimeRemainin reads "10分钟内" for anything not ahead), but a past
# time shown as the next timer end misleads in HA; the coordinator re-reads
# the full state shortly after each end (TIMER_CHECK_DELAY) to learn what
# the cloud says now.
TIMER_FIRED_MS = 10 * 60 * 1000


def has_simple_timer(status: dict[str, Any]) -> bool:
    """The controller reports the timer fields (even as null)."""
    return any(key in status for key in TIMER_KEYS)


def timer_active(status: dict[str, Any]) -> bool:
    """hasTimer = 1 === timerSimpleFlg (HA also accepts "1").

    A missing or null flag is no timer, as the page's model default 0.
    """
    return is_on(status.get(KEY_TIMER_FLAG))


def timer_turns_on(status: dict[str, Any]) -> bool:
    """1 === timerSimpleOnoff: the timer turns the unit on ("开机"), else off."""
    return is_on(status.get(KEY_TIMER_ONOFF))


def new_timer_turns_on(status: dict[str, Any]) -> bool:
    """A new timer always toggles the current power state (timer_switch).

    The page sends ``timer_switch: isOn ? "0" : "1"``: a running unit gets an
    off timer, a stopped one an on timer.
    """
    return not is_on(status.get(KEY_POWER))


def timer_option(hours: float) -> str:
    """Select option id of a duration: 0.5 -> "0_5h", 12 -> "12h"."""
    return f"{hours:g}".replace(".", "_") + "h"


TIMER_OPTIONS: dict[str, float] = {timer_option(h): h for h in TIMER_HOURS}


def timer_end_ms(status: dict[str, Any], now_ms: float) -> int | None:
    """When the active timer fires (epoch ms), or None.

    The page shows the time from timerSimpleTime with ``new Date(t)`` and
    the remaining time as ``(timerSimpleTime - now) / 1.8e6`` half hours
    (getTimeRemainin; the standard page passes the phone clock as now, so
    timerSimpleTime is epoch ms on the same clock). None without an active
    timer, for a value that is not a number or lies more than 25 h ahead,
    and for an end that has already fired (more than TIMER_FIRED_MS ago).
    """
    if not timer_active(status):
        return None
    end = as_number(status.get(KEY_TIMER_TIME))
    if end is None or not now_ms - TIMER_FIRED_MS <= end <= now_ms + _TIMER_END_WINDOW_MS:
        return None
    return int(end)


def timer_fired(status: dict[str, Any], now_ms: float) -> bool:
    """The cloud still reports a timer whose end passed over TIMER_FIRED_MS ago.

    Also true for a nonsense end in the past (0, seconds instead of ms):
    either way, the running timer's duration and action cannot be trusted.
    """
    if not timer_active(status):
        return False
    end = as_number(status.get(KEY_TIMER_TIME))
    return end is not None and end < now_ms - TIMER_FIRED_MS


def timer_set_state(hours: float, turn_on: bool, data: dict[str, Any]) -> dict[str, Any]:
    """Local state after an accepted timer, as the page updates its model.

    hasTimer = true, timerSimpleOnoff = isOn ? 0 : 1 and timerSimpleTime
    from the answer. Without a number there the end is unknown (None) rather
    than the previous timer's; simple_timer_time is left to the reports (the
    app never reads it back).
    """
    end = as_number(data.get(KEY_TIMER_TIME))
    return {
        KEY_TIMER_FLAG: 1,
        KEY_TIMER_ONOFF: 1 if turn_on else 0,
        KEY_TIMER_TIME: None if end is None else int(end),
    }


def timer_cancel_state(data: dict[str, Any]) -> dict[str, Any]:
    """Local state after a cancel: hasTimer = false (timerSimpleTime from the answer)."""
    state: dict[str, Any] = {KEY_TIMER_FLAG: 0}
    if (end := as_number(data.get(KEY_TIMER_TIME))) is not None:
        state[KEY_TIMER_TIME] = int(end)
    return state
