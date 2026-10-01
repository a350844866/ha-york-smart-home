"""Read-only running information decoded like the app. No Home Assistant imports.

Status banners (xkq-color-control / xkq-control pages), the running state
of the home page's masking overlay (getXkqMaskingFlg), the fault list
(service getIsAlarm / getAlarmInfo) and the fresh-air controller's air
quality tiles.
"""

from __future__ import annotations

import math
from typing import Any

from .api import as_number, is_on
from .const import (
    ALARM_SLOTS,
    ALARM_UNUSED_SLOT,
    HUMAN_SENSE_3D,
    KEY_COMFORTABLE,
    KEY_COMMUNICATION_EXCEPTION,
    KEY_ENERGY_SAVING,
    KEY_HUMAN_SENSE_TYPE2,
    KEY_INDOOR_CO2,
    KEY_INDOOR_HUMIDITY,
    KEY_INDOOR_PM25,
    KEY_INDOOR_TVOC,
    KEY_OTA,
    KEY_POWER,
    KEY_SELF_CLEAN,
    KEY_STERILIZATION,
    RUNNING_COMFORTABLE,
    RUNNING_COOLING_DOWN,
    RUNNING_ENERGY_SAVING,
    RUNNING_IDLE,
    RUNNING_OTA,
    RUNNING_SELF_CLEAN,
    RUNNING_STERILIZING,
    SELF_CLEAN_COOLING_DOWN,
    SELF_CLEAN_RUNNING,
)
from .profile import Profile, fresh_air
from .trouble_codes import describe_code


def _equals(value: Any, number: int) -> bool:
    """JavaScript's loose ``value == number`` for the report's value types."""
    if isinstance(value, bool):
        return int(value) == number
    return as_number(value) == number


# ----------------------------------------------------------- status banners


def has_banner(status: dict[str, Any], key: str) -> bool:
    """Whether the controller reports a banner key (and it can apply).

    The app shows 人感通讯异常 only with the 3D human sensor
    ("2" === human_sense_type2); without it the flag means nothing. The app
    reads human_sense_type2 from the controller constants only; ``status``
    is the view (constants laid over the live status), so a controller
    without that constant falls back to a reported value, which the app
    would ignore. Constants normally carry the key (the fixture has "0").
    """
    if status.get(key) is None:
        return False
    if key == KEY_COMMUNICATION_EXCEPTION:
        return str(status.get(KEY_HUMAN_SENSE_TYPE2)) == HUMAN_SENSE_3D
    return True


def banner_on(status: dict[str, Any], key: str) -> bool | None:
    """A banner flag: on for 1, None when missing or not a number.

    The app compares "1" === value; the report sends strings, but like the
    rest of the integration integer and boolean flags are accepted too.
    """
    value = status.get(key)
    if value is None:
        return None
    # Before any value check: without the 3D human sensor the app never
    # shows the banner, whatever the flag (allowShowTX).
    if key == KEY_COMMUNICATION_EXCEPTION and not has_banner(status, key):
        return False
    if isinstance(value, bool):
        return value
    number = as_number(value)
    if number is None:
        return None
    return number == 1


# ------------------------------------------------------------ running state


def cooling_down(status: dict[str, Any]) -> bool:
    """self_clean == 2: the system cools down after self cleaning.

    The control page then shows its self-clean dialog (status 2), which only
    leaves the page: the controller cannot be controlled meanwhile. Same
    priority as the app: a running self clean (1) or sterilization (the
    control page checks it before self_clean == 2 and opens its own dialog,
    which can stop it) wins, so this follows running_state.
    """
    return running_state(status) == RUNNING_COOLING_DOWN


def running_state(status: dict[str, Any]) -> str:
    """The home page's overlay text (getXkqMaskingFlg), as a state id.

    Same order as the app: self_clean == 1, hig_temperature_sterilization
    === 1, self_clean == 2, otaFlg == 1, "1" == comfortable,
    "1" == energy_saving. The app compares the sterilization flag strictly
    with the number 1 (the controller reports it as a number); a string "1"
    is accepted here as well, like every other flag of the integration.
    """
    if _equals(status.get(KEY_SELF_CLEAN), SELF_CLEAN_RUNNING):
        return RUNNING_SELF_CLEAN
    if _equals(status.get(KEY_STERILIZATION), 1):
        return RUNNING_STERILIZING
    if _equals(status.get(KEY_SELF_CLEAN), SELF_CLEAN_COOLING_DOWN):
        return RUNNING_COOLING_DOWN
    if _equals(status.get(KEY_OTA), 1):
        return RUNNING_OTA
    if _equals(status.get(KEY_COMFORTABLE), 1):
        return RUNNING_COMFORTABLE
    if _equals(status.get(KEY_ENERGY_SAVING), 1):
        return RUNNING_ENERGY_SAVING
    return RUNNING_IDLE


# ------------------------------------------------------------------- faults


def _js_truthy(value: Any) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return True


def raw_alarm(status: dict[str, Any]) -> bool:
    """The app's red alarm icon (getIsAlarm).

    Any iu<n>_alarm_code that is truthy in JavaScript and not the string
    "0". Unlike the alarm list it does not skip unused (255) slots.
    """
    return any(
        _js_truthy(value) and value != "0"
        for value in (status.get(f"iu{n}_alarm_code") for n in ALARM_SLOTS)
    )


def alarms(status: dict[str, Any]) -> list[dict[str, str]] | None:
    """Active alarms, decoded like the app's getAlarmInfo.

    The code is the reported decimal value as two upper-case hex digits (as
    the app shows it), with the app's description when it has one. Slots
    whose system or address number is 255 are unused and skipped. Like the
    app (Number(undefined) is NaN, not 255) a slot without those numbers is
    still listed, just without system_no / indoor_unit_no. None when
    the report has no usable alarm fields: "no data" must not read as
    "no fault".
    """
    result = []
    seen = False
    for n in ALARM_SLOTS:
        raw = status.get(f"iu{n}_alarm_code")
        if raw is None:
            continue
        code = as_number(raw)
        if code is None:
            return None
        seen = True
        if code == 0:
            continue
        system_no = as_number(status.get(f"iu{n}_system_no"))
        addr_no = as_number(status.get(f"iu{n}_addr_no"))
        if ALARM_UNUSED_SLOT in (system_no, addr_no):
            continue
        hex_code = f"{int(code):02X}"
        alarm = {"code": hex_code}
        if system_no is not None:
            alarm["system_no"] = f"{int(system_no):02d}"
        if addr_no is not None:
            alarm["indoor_unit_no"] = f"{int(addr_no):02d}"
        result.append({**alarm, **describe_code(hex_code)})
    return result if seen else None


# ------------------------------------------------------------- air quality

# The fresh-air colour controller's tiles (室内湿度 %, 室内PM2.5 ug/m3,
# 室内CO2 ppm, 室内TVOC as a level: 0 good, 1 medium, >= 2 bad). The plain
# colour page never shows them (the author's unit reports 0 for all of them).
# key -> plausible range; 0 % humidity and 0 ppm CO2 are not readings.
AIR_QUALITY_RANGES: dict[str, tuple[float, float]] = {
    KEY_INDOOR_HUMIDITY: (1, 100),
    KEY_INDOOR_PM25: (0, 1000),
    KEY_INDOOR_CO2: (1, 10000),
    KEY_INDOOR_TVOC: (0, 10),
}


def air_value(status: dict[str, Any], key: str) -> float | None:
    """A reported air-quality value, or None if missing or implausible."""
    value = as_number(status.get(key))
    low, high = AIR_QUALITY_RANGES[key]
    if value is None or not low <= value <= high:
        return None
    return int(value) if value == int(value) else value


def air_reading(status: dict[str, Any], profile: Profile, key: str) -> float | None:
    """What the fresh-air page shows: the value while on, "--" (None) while off.

    None on controllers that are not fresh-air controllers.
    """
    if not fresh_air(status, profile) or not is_on(status.get(KEY_POWER)):
        return None
    return air_value(status, key)
