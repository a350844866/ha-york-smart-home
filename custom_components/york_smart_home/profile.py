"""Differences between the two wire controller pages. No Home Assistant imports.

The home page opens xkq-color-control (chunk 41, service q8O6 in chunk 3) for
the colour controller (xkqType "9") and xkq-control (chunk 44, service yNUN
in main) for the standard one (xkqType "8"). Both send the same ctrlJson
vocabulary through the same endpoints, but their rules differ in details:
the modes offered, when an extra function is offered, which functions a
change cancels, the exclusion lists, what power off keeps, the human
sensing, the status banners, the filter reminder and the settings page.
Each difference is a field here, with the app code it comes from.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .api import XKQ_TYPE_STANDARD, is_on
from .const import (
    FUNC_AUX_HEAT,
    FUNC_CLEAN,
    FUNC_FOREST,
    FUNC_HEALTH,
    FUNC_HUMIDIFY,
    FUNC_LEAVE,
    FUNC_MUTE,
    FUNC_NO_WIND,
    FUNC_PEOPLE,
    FUNC_PEOPLE_ONE,
    FUNC_SAVE,
    FUNC_SLEEP,
    FUNC_SMART_PEOPLE,
    FUNC_STERILIZE,
    FUNC_STRONG,
    FUNC_WIND_BLOW,
    FUNC_WIND_SHELTER,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_DRY_AUTO,
    MODE_DRY_COMFORT,
    MODE_DRY_FLOOR_HEAT,
    MODE_DRY_STRONG,
    MODE_FAN,
    MODE_FAN_FLOOR_HEAT,
    MODE_FLOOR_HEAT,
    MODE_FRESH_AIR_ECO,
    MODE_HEAT,
    MODE_HEAT_FLOOR_HEAT,
    MODE_HEAT_SUPPLY,
    MODE_PURIFY,
    MODES,
    STANDARD_STATUS_BANNERS,
    STATUS_BANNERS,
)

# Extra functions (allFuncArr): id -> (control key, availability key). Both
# pages use the same keys; the standard page offers fewer of them and reads
# human sensing from human_sense_type (see Profile.funcs).
FUNC_KEYS: dict[str, tuple[str, str]] = {
    FUNC_SAVE: ("energy_conservation", "is_energy_conservation_avalable"),
    FUNC_SLEEP: ("sleep", "is_sleep_avalable"),
    FUNC_MUTE: ("indoor_unit_silent", "is_indoor_unit_silent_avalable"),
    FUNC_HEALTH: ("health", "is_health_avalable"),
    FUNC_FOREST: ("forest_wind", "is_forest_wind_avalable"),
    FUNC_AUX_HEAT: ("auxiliary_heat", "is_auxiliary_heat_avalable"),
    FUNC_CLEAN: ("self_clean", "is_self_clean_avalable"),
    FUNC_STRONG: ("force_switch", "is_force_avalable"),
    FUNC_NO_WIND: ("no_sense_of_wind", "is_nosense_wind_avalable"),
    FUNC_PEOPLE_ONE: ("one_direction_human_sense_effect", "has_people_one"),
    FUNC_PEOPLE: ("independent_human_sense_effect", "has_people_independent_K3"),
    FUNC_LEAVE: ("away_home_energy_saving", "is_awayhome_saveenergy_avalable"),
    FUNC_STERILIZE: ("hig_temperature_sterilization", "hig_temperature_sterilization_fix"),
    FUNC_HUMIDIFY: ("humidification", "is_humidification_avalable"),
    FUNC_SMART_PEOPLE: ("human_sensing_control_3d", "has_smart_people_feeling"),
    FUNC_WIND_BLOW: ("one_direction_human_sense", "has_smart_people_feeling"),
    FUNC_WIND_SHELTER: ("one_direction_human_sense", "has_smart_people_feeling"),
}
# Sent as integers 1/0 by control(); every other flag is "1"/"0".
INT_FUNCS = frozenset({FUNC_CLEAN, FUNC_STERILIZE})
# Human-sensing entries: the colour page's control() skips them when it
# sends the function flags and sends their own keys instead.
HUMAN_FUNCS = frozenset({FUNC_PEOPLE_ONE, FUNC_PEOPLE, FUNC_WIND_BLOW, FUNC_WIND_SHELTER})
# Functions shown as simple on/off switches (the human sensing has selects).
SWITCH_FUNCS = tuple(f for f in FUNC_KEYS if f not in HUMAN_FUNCS)

FLOOR_MODES = frozenset(
    {MODE_FLOOR_HEAT, MODE_DRY_FLOOR_HEAT, MODE_HEAT_FLOOR_HEAT, MODE_FAN_FLOOR_HEAT}
)

_ALL_OFF = frozenset(
    {
        FUNC_SAVE,
        FUNC_MUTE,
        FUNC_SLEEP,
        FUNC_HEALTH,
        FUNC_AUX_HEAT,
        FUNC_FOREST,
        FUNC_NO_WIND,
        FUNC_LEAVE,
        FUNC_STRONG,
        FUNC_PEOPLE,
        FUNC_PEOPLE_ONE,
        FUNC_SMART_PEOPLE,
        FUNC_WIND_BLOW,
        FUNC_WIND_SHELTER,
        FUNC_CLEAN,
        FUNC_STERILIZE,
    }
)

# xkq-color-control funcsChanged: turning the key on deselects these. PEOPLE
# and PEOPLE_ONE have a case there too, but funcsChanged returns early for
# them (their modals); humanFeelChanged / oneHumanFeelChanged deselect only
# CLEAN, HIGH_TEMP_STERILIZE and NO_WIND. SMART_PEOPLE_FEELING's list is
# applied after its confirm dialog (showSmartPeopleFeelingModal repeats it).
# HUMI opens its modal and deselects nothing.
_COLOR_EXCLUSIONS: dict[str, frozenset[str]] = {
    FUNC_SAVE: frozenset(
        {
            FUNC_MUTE,
            FUNC_SLEEP,
            FUNC_AUX_HEAT,
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_STRONG,
            FUNC_SMART_PEOPLE,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_SLEEP: frozenset(
        {
            FUNC_SAVE,
            FUNC_MUTE,
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_STRONG,
            FUNC_SMART_PEOPLE,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_MUTE: frozenset(
        {
            FUNC_SAVE,
            FUNC_SLEEP,
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_STRONG,
            FUNC_SMART_PEOPLE,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_HEALTH: frozenset({FUNC_LEAVE, FUNC_CLEAN, FUNC_STERILIZE}),
    FUNC_AUX_HEAT: frozenset(
        {
            FUNC_SAVE,
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_SMART_PEOPLE,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_FOREST: frozenset(
        {
            FUNC_SAVE,
            FUNC_MUTE,
            FUNC_SLEEP,
            FUNC_AUX_HEAT,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_STRONG,
            FUNC_SMART_PEOPLE,
            FUNC_WIND_BLOW,
            FUNC_WIND_SHELTER,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_NO_WIND: _ALL_OFF - {FUNC_HEALTH, FUNC_NO_WIND},
    FUNC_LEAVE: _ALL_OFF - {FUNC_LEAVE},
    FUNC_STRONG: frozenset(
        {
            FUNC_SAVE,
            FUNC_MUTE,
            FUNC_SLEEP,
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_SMART_PEOPLE,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_PEOPLE: frozenset({FUNC_CLEAN, FUNC_STERILIZE, FUNC_NO_WIND}),
    FUNC_PEOPLE_ONE: frozenset({FUNC_CLEAN, FUNC_STERILIZE, FUNC_NO_WIND}),
    FUNC_SMART_PEOPLE: frozenset(
        {
            FUNC_SAVE,
            FUNC_MUTE,
            FUNC_SLEEP,
            FUNC_AUX_HEAT,
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_STRONG,
            FUNC_WIND_BLOW,
            FUNC_WIND_SHELTER,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_WIND_BLOW: frozenset(
        {
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_SMART_PEOPLE,
            FUNC_WIND_SHELTER,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    FUNC_WIND_SHELTER: frozenset(
        {
            FUNC_FOREST,
            FUNC_NO_WIND,
            FUNC_LEAVE,
            FUNC_SMART_PEOPLE,
            FUNC_WIND_BLOW,
            FUNC_CLEAN,
            FUNC_STERILIZE,
        }
    ),
    # Everything except HUMI (and itself).
    FUNC_CLEAN: _ALL_OFF - {FUNC_CLEAN},
    FUNC_STERILIZE: _ALL_OFF - {FUNC_STERILIZE},
    FUNC_HUMIDIFY: frozenset(),
}

# xkq-control funcsChanged (standard page). Shorter lists: there is no
# SMART_PEOPLE / WIND_BLOW / WIND_SHELTER / PEOPLE_ONE, LEAVE has no case
# (default: nothing) and is not deselected by the others. PEOPLE returns
# early to its modal (humanFeelChanged: CLEAN, HIGH_TEMP_STERILIZE, NO_WIND).
_STANDARD_EXCLUSIONS: dict[str, frozenset[str]] = {
    FUNC_SAVE: frozenset(
        {
            FUNC_SLEEP,
            FUNC_MUTE,
            FUNC_FOREST,
            FUNC_AUX_HEAT,
            FUNC_CLEAN,
            FUNC_STRONG,
            FUNC_NO_WIND,
            FUNC_STERILIZE,
        }
    ),
    FUNC_SLEEP: frozenset(
        {FUNC_SAVE, FUNC_MUTE, FUNC_FOREST, FUNC_CLEAN, FUNC_STRONG, FUNC_NO_WIND, FUNC_STERILIZE}
    ),
    FUNC_MUTE: frozenset(
        {FUNC_SAVE, FUNC_SLEEP, FUNC_FOREST, FUNC_CLEAN, FUNC_STRONG, FUNC_NO_WIND, FUNC_STERILIZE}
    ),
    FUNC_HEALTH: frozenset({FUNC_CLEAN, FUNC_STERILIZE}),
    FUNC_FOREST: frozenset(
        {
            FUNC_SAVE,
            FUNC_SLEEP,
            FUNC_MUTE,
            FUNC_AUX_HEAT,
            FUNC_CLEAN,
            FUNC_STRONG,
            FUNC_NO_WIND,
            FUNC_STERILIZE,
        }
    ),
    FUNC_AUX_HEAT: frozenset({FUNC_CLEAN, FUNC_NO_WIND, FUNC_SAVE, FUNC_FOREST, FUNC_STERILIZE}),
    FUNC_CLEAN: frozenset(
        {
            FUNC_SAVE,
            FUNC_SLEEP,
            FUNC_MUTE,
            FUNC_HEALTH,
            FUNC_FOREST,
            FUNC_AUX_HEAT,
            FUNC_STRONG,
            FUNC_NO_WIND,
            FUNC_PEOPLE,
            FUNC_STERILIZE,
        }
    ),
    FUNC_STRONG: frozenset(
        {FUNC_SAVE, FUNC_SLEEP, FUNC_MUTE, FUNC_FOREST, FUNC_CLEAN, FUNC_NO_WIND, FUNC_STERILIZE}
    ),
    FUNC_NO_WIND: frozenset(
        {
            FUNC_SAVE,
            FUNC_SLEEP,
            FUNC_MUTE,
            FUNC_FOREST,
            FUNC_AUX_HEAT,
            FUNC_CLEAN,
            FUNC_STRONG,
            FUNC_STERILIZE,
            FUNC_PEOPLE,
        }
    ),
    FUNC_PEOPLE: frozenset({FUNC_CLEAN, FUNC_STERILIZE, FUNC_NO_WIND}),
    FUNC_STERILIZE: frozenset(
        {
            FUNC_SAVE,
            FUNC_SLEEP,
            FUNC_MUTE,
            FUNC_HEALTH,
            FUNC_FOREST,
            FUNC_AUX_HEAT,
            FUNC_STRONG,
            FUNC_CLEAN,
            FUNC_NO_WIND,
            FUNC_PEOPLE,
        }
    ),
    FUNC_LEAVE: frozenset(),
}


@dataclass(frozen=True, kw_only=True)
class Profile:
    """The rules of one controller page."""

    key: str
    # Modes of the page's service allModeArr (app order kept from MODES).
    modes: tuple[str, ...]
    # getTempLimit: modes using the heating range (the rest of its cases use
    # the cooling range, AUTO 16-32, anything else 1-99).
    heat_range: frozenset[str]
    # allFuncArr entries of the service, app order.
    funcs: tuple[str, ...]
    # getFuncArr: colour '"1" === statusKey'; standard '"0" !== statusKey &&
    # undefined !== statusKey' (on a model whose defaults are "0").
    func_offered_strict: bool
    exclusions: dict[str, frozenset[str]]
    # cancelFuncSelected("temp"/"humi"), ("wind"), ("direction").
    cancel_on_temp: frozenset[str]
    cancel_on_fan: frozenset[str]
    cancel_on_direction: frozenset[str]
    # switchChanged: functions kept when turning off.
    kept_on_power_off: frozenset[str]
    # switchChanged / modeChanged / directionChanged call closeOneHumanFeeling.
    closes_one_direction: bool
    # control(): the sterilization flag when off (colour 0, standard "0").
    sterilize_off: Any
    # control() sends PEOPLE's flag (independent_human_sense_effect) with the
    # other function flags (the standard page does not skip it).
    people_in_flags: bool
    # refreshFuncEnable: the colour page also disables most functions in CLEAR
    # (purification) and the human sensing while allowShowTX; the standard
    # page does not know CLEAR there and requires "not temp fixed" for LEAVE.
    color_enable_rules: bool
    # getWindArr result emptied when "auto" is the only speed (colour page).
    hide_auto_only_fan: bool
    # ionViewWillEnter: one horizontal deflector without 3D/far wind forces
    # unified control (colour page only).
    forces_unify: bool
    # getDirectionUnifyMax / getDirectionIndependentMax (3D/far units).
    unify_max_modes: frozenset[str]
    special_independent_max_modes: frozenset[str]
    # Status banners: key -> enabled by default.
    banners: dict[str, bool]
    # A fresh-air colour controller (saveene_freshair "1") is possible.
    fresh_air: bool
    # allowShowLW: only filter_life_tips1 == 2 on the standard page.
    filter_all_slots: bool


COLOR = Profile(
    key="color",
    modes=tuple(MODES),
    heat_range=frozenset(
        {
            MODE_HEAT,
            MODE_FLOOR_HEAT,
            MODE_DRY_FLOOR_HEAT,
            MODE_FAN_FLOOR_HEAT,
            MODE_HEAT_FLOOR_HEAT,
            MODE_HEAT_SUPPLY,
            MODE_PURIFY,
        }
    ),
    funcs=tuple(FUNC_KEYS),
    func_offered_strict=True,
    exclusions=_COLOR_EXCLUSIONS,
    cancel_on_temp=frozenset({FUNC_SAVE, FUNC_SMART_PEOPLE}),
    cancel_on_fan=frozenset(
        {FUNC_SAVE, FUNC_MUTE, FUNC_FOREST, FUNC_STRONG, FUNC_NO_WIND, FUNC_SMART_PEOPLE}
    ),
    cancel_on_direction=frozenset(
        {FUNC_NO_WIND, FUNC_FOREST, FUNC_SMART_PEOPLE, FUNC_WIND_BLOW, FUNC_WIND_SHELTER}
    ),
    kept_on_power_off=frozenset(),
    closes_one_direction=True,
    sterilize_off=0,
    people_in_flags=False,
    color_enable_rules=True,
    hide_auto_only_fan=True,
    forces_unify=True,
    unify_max_modes=frozenset(
        {MODE_HEAT, MODE_FAN, MODE_HEAT_FLOOR_HEAT, MODE_FAN_FLOOR_HEAT, MODE_HEAT_SUPPLY}
    ),
    special_independent_max_modes=frozenset(
        {MODE_HEAT, MODE_FAN, MODE_HEAT_FLOOR_HEAT, MODE_HEAT_SUPPLY}
    ),
    banners=STATUS_BANNERS,
    fresh_air=True,
    filter_all_slots=True,
)

STANDARD = Profile(
    key="standard",
    # yNUN allModeArr: no 节能新风 (SAVE_WIND) and no 供热 (HEATING).
    modes=tuple(m for m in MODES if m not in (MODE_FRESH_AIR_ECO, MODE_HEAT_SUPPLY)),
    # yNUN getTempLimit has no CLEAR case: purification falls back to 1-99.
    heat_range=frozenset(
        {MODE_HEAT, MODE_FLOOR_HEAT, MODE_DRY_FLOOR_HEAT, MODE_FAN_FLOOR_HEAT, MODE_HEAT_FLOOR_HEAT}
    ),
    funcs=(
        FUNC_SAVE,
        FUNC_SLEEP,
        FUNC_MUTE,
        FUNC_HEALTH,
        FUNC_FOREST,
        FUNC_AUX_HEAT,
        FUNC_CLEAN,
        FUNC_STRONG,
        FUNC_NO_WIND,
        FUNC_PEOPLE,
        FUNC_LEAVE,
        FUNC_STERILIZE,
    ),
    func_offered_strict=False,
    exclusions=_STANDARD_EXCLUSIONS,
    cancel_on_temp=frozenset({FUNC_SAVE}),
    cancel_on_fan=frozenset({FUNC_SAVE, FUNC_MUTE, FUNC_FOREST, FUNC_STRONG, FUNC_NO_WIND}),
    cancel_on_direction=frozenset({FUNC_NO_WIND, FUNC_FOREST}),
    kept_on_power_off=frozenset({FUNC_CLEAN}),
    closes_one_direction=False,
    sterilize_off="0",
    people_in_flags=True,
    color_enable_rules=False,
    hide_auto_only_fan=False,
    forces_unify=False,
    unify_max_modes=frozenset({MODE_HEAT, MODE_FAN, MODE_HEAT_FLOOR_HEAT, MODE_FAN_FLOOR_HEAT}),
    special_independent_max_modes=frozenset({MODE_HEAT, MODE_FAN, MODE_HEAT_FLOOR_HEAT}),
    banners=STANDARD_STATUS_BANNERS,
    fresh_air=False,
    filter_all_slots=False,
)

# getDirectionIndependentMax without 3D/far wind (the same on both pages).
INDEPENDENT_MAX_MODES = frozenset(
    {MODE_COOL, MODE_DRY, MODE_HEAT, MODE_FAN, MODE_AUTO, MODE_HEAT_FLOOR_HEAT, MODE_DRY_AUTO}
)
# getHumiLimit: 35-90 in these modes.
DRY_HUMIDITY_MODES = frozenset(
    {MODE_DRY_FLOOR_HEAT, MODE_DRY_AUTO, MODE_DRY_COMFORT, MODE_DRY_STRONG}
)
# refreshTempEnable / refreshHumiEnable: humidity instead of temperature.
HUMIDITY_MODES = frozenset({MODE_DRY_AUTO, MODE_DRY_FLOOR_HEAT})


def profile_for(xkq_type: str) -> Profile:
    """The page the app's home page opens for this xkqType ("8" standard, else colour)."""
    return STANDARD if xkq_type == XKQ_TYPE_STANDARD else COLOR


def js_truthy(value: Any) -> bool:
    """JavaScript truthiness of a reported value."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return True


def model_str(value: Any) -> str:
    """A string field of the page's model: falsy reports keep the default "0".

    The pages copy a reported value into their model only if it is truthy
    (``xkqStatusListGjy[0][t] && (model[t] = ...)``); integers are compared
    as the strings they stand for.
    """
    if not js_truthy(value):
        return "0"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def offered(status: dict[str, Any], profile: Profile, key: str) -> bool:
    """getFuncArr's availability test for one statusKey."""
    value = status.get(key)
    if profile.func_offered_strict:
        return is_on(value)
    # The standard page copies only truthy constants over its "0" defaults.
    return js_truthy(value) and model_str(value) != "0"


def human_sense_type(status: dict[str, Any]) -> str:
    return model_str(status.get("human_sense_type"))


def has_smart_people(status: dict[str, Any]) -> bool:
    """has_smart_people_feeling, derived by the colour page on entry."""
    return (
        model_str(status.get("human_sense_type2")) == "2"
        and not is_on(status.get("set_temp_fixed"))
        and not is_on(status.get("B9"))
    )


def func_offered(status: dict[str, Any], profile: Profile, func: str) -> bool:
    """Whether the page lists this function (allFuncArr filtered by getFuncArr)."""
    if func not in profile.funcs:
        return False
    if profile is COLOR:
        # Derived flags (ionViewWillEnter), not constants.
        if func == FUNC_PEOPLE_ONE:
            return human_sense_type(status) == "1"
        if func == FUNC_PEOPLE:
            return human_sense_type(status) in ("2", "3")
        if func in (FUNC_SMART_PEOPLE, FUNC_WIND_BLOW, FUNC_WIND_SHELTER):
            return has_smart_people(status)
    elif func == FUNC_PEOPLE:
        # yNUN: {statusKey: "human_sense_type", controlKey: independent_...}.
        return offered(status, profile, "human_sense_type")
    return offered(status, profile, FUNC_KEYS[func][1])


def offered_funcs(status: dict[str, Any], profile: Profile) -> list[str]:
    return [f for f in profile.funcs if func_offered(status, profile, f)]


def func_active(status: dict[str, Any], func: str) -> bool:
    """getFuncs: the control key loosely equal to 1 ("1" == value).

    WIND_BLOW / WIND_SHELTER read one_direction_human_sense strictly as "1" /
    "2"; PEOPLE_ONE's key one_direction_human_sense_effect is set by the page
    when one_direction_human_sense is not "0".
    """
    if func == FUNC_WIND_BLOW:
        return model_str(status.get("one_direction_human_sense")) == "1"
    if func == FUNC_WIND_SHELTER:
        return model_str(status.get("one_direction_human_sense")) == "2"
    if func == FUNC_PEOPLE_ONE:
        # Deliberately only the direction: getFuncs reads the effect flag,
        # which the page also copies from a truthy report. HA never sends
        # that flag (neither does the app's control()), so a reported "1"
        # could stay in the merged state after the direction is set to "0"
        # and pin the function as selected. No known controller reports the
        # effect "1" with the direction "0".
        return model_str(status.get("one_direction_human_sense")) != "0"
    return is_on(status.get(FUNC_KEYS[func][0]))


def active_funcs(status: dict[str, Any], profile: Profile) -> set[str]:
    return {f for f in offered_funcs(status, profile) if func_active(status, f)}


def human_sense_error(status: dict[str, Any], profile: Profile) -> bool:
    """allowShowTX: 人感通讯异常 with the 3D sensor (colour page only)."""
    return (
        profile.color_enable_rules
        and model_str(status.get("communication_exception")) == "1"
        and model_str(status.get("human_sense_type2")) == "2"
    )


def func_enabled(
    status: dict[str, Any], profile: Profile, func: str, *, on: bool, mode: str | None
) -> bool:
    """refreshFuncEnable: whether the page lets the user toggle the function now.

    ``on`` / ``mode`` are the page's current power and mode. Locks are the
    controller constants (set_temp_fixed, B9).
    """
    temp_fixed = is_on(status.get("set_temp_fixed"))
    fan_fixed = is_on(status.get("B9"))
    color = profile.color_enable_rules
    # The colour page also lists CLEAR (purification) wherever it lists the
    # floor modes; the standard page does not.
    blocked = FLOOR_MODES | {MODE_PURIFY} if color else FLOOR_MODES
    tx = human_sense_error(status, profile)
    if func in (FUNC_SAVE, FUNC_SLEEP):
        return not (temp_fixed or fan_fixed) and on and mode not in blocked | {MODE_FAN, MODE_AUTO}
    if func == FUNC_MUTE:
        return not fan_fixed and on and mode not in blocked | {MODE_AUTO}
    if func == FUNC_HEALTH:
        return on and mode not in blocked
    if func == FUNC_FOREST:
        return on and mode == MODE_COOL
    if func == FUNC_AUX_HEAT:
        return on and mode == MODE_HEAT
    if func == FUNC_CLEAN:
        return not on and mode not in blocked
    if func == FUNC_STRONG:
        return on and mode in (MODE_COOL, MODE_HEAT)
    if func == FUNC_NO_WIND:
        return not fan_fixed and on and mode not in blocked | {MODE_AUTO}
    if func in (FUNC_PEOPLE, FUNC_PEOPLE_ONE, FUNC_WIND_BLOW, FUNC_WIND_SHELTER):
        return on and not tx and mode not in blocked
    if func == FUNC_LEAVE:
        return on and mode == MODE_FLOOR_HEAT and (color or not temp_fixed)
    if func == FUNC_STERILIZE:
        return not on and mode not in (blocked | {MODE_DRY_AUTO} if color else blocked)
    if func in (FUNC_HUMIDIFY, FUNC_SMART_PEOPLE):
        # HUMI has no `break` and falls through to SMART_PEOPLE_FEELING's rule.
        return on and not tx and mode not in blocked | {MODE_DRY_AUTO}
    return False


def fresh_air(status: dict[str, Any], profile: Profile) -> bool:
    """xkqColorType WIND: a fresh-air colour controller ("1" === saveene_freshair)."""
    return profile.fresh_air and is_on(status.get("saveene_freshair"))
