"""Command building, checked against the app's control page behavior."""

from __future__ import annotations

import pytest

from custom_components.york_smart_home.const import MODE_GROUP, MODES
from custom_components.york_smart_home.control import (
    build_command,
    current_mode,
    encode_temperature,
    has_fan,
    has_temperature,
    humidity_control,
    humidity_limits,
    supported_modes,
    temp_limits,
)
from custom_components.york_smart_home.profile import COLOR, STANDARD

from .conftest import FIXTURE


def _status(**overrides) -> dict:
    return {**FIXTURE["device"]["constantJson"], **FIXTURE["status"], **overrides}


# Fixture state: on, cool, 27.5 °C, low fan, no extra functions available.


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (27.0, {"set_temp": 27, "set_temperature_add05": "0"}),
        (27.5, {"set_temp": 27, "set_temperature_add05": "1"}),
        (16.0, {"set_temp": 16, "set_temperature_add05": "0"}),
    ],
)
def test_encode_temperature(temp: float, expected: dict) -> None:
    assert encode_temperature(temp) == expected


def test_only_changed_fields_are_sent() -> None:
    status = _status()
    assert build_command(status, {"temperature": 28.0}) == {
        "set_temp": 28,
        "set_temperature_add05": "0",
    }
    assert build_command(status, {"fan": "high"}) == {"wire_controller_wind_high": "1"}
    assert build_command(status, {"mode": "heat"}) == {"wire_controller_heating": "1"}
    assert build_command(status, {"power": False}) == {"if_run": "0"}


def test_nothing_to_send() -> None:
    status = _status()
    assert build_command(status, {}) == {}
    assert build_command(status, {"power": True, "mode": "cool"}) == {}
    assert build_command(status, {"temperature": 27.5, "fan": "low"}) == {}


def test_merged_changes() -> None:
    """Changes collected in one debounce window go out together, in app order."""
    status = _status(if_run="0")
    assert build_command(
        status, {"power": True, "mode": "dry", "temperature": 26.0, "fan": "medium"}
    ) == {
        "if_run": "1",
        "wire_controller_rmwet": "1",
        "set_temp": 26,
        "set_temperature_add05": "0",
        "wire_controller_wind_mid": "1",
    }


def test_mode_change_clamps_setpoint() -> None:
    # modeChanged clamps the setpoint into the new mode's range and sends it.
    status = _status(set_temp=16, set_temperature_add05="0", wire_controller_makecold="0")
    status["wire_controller_heating"] = "1"
    assert build_command(status, {"mode": "cool"}) == {
        "wire_controller_makecold": "1",
        "set_temp": 19,
        "set_temperature_add05": "0",
    }


def test_explicit_temperature_clamped_to_new_mode() -> None:
    # The slider's min/max bound tempChanged: a setpoint below the cooling
    # range sent together with the mode change is clamped, not sent as is.
    status = _status(set_temp=16, set_temperature_add05="0", wire_controller_makecold="0")
    status["wire_controller_heating"] = "1"
    assert build_command(status, {"power": True, "mode": "cool", "temperature": 16.0}) == {
        "wire_controller_makecold": "1",
        "set_temp": 19,
        "set_temperature_add05": "0",
    }
    # Above the range in the current mode (cooling, 19-30).
    assert build_command(_status(), {"temperature": 35.0}) == {
        "set_temp": 30,
        "set_temperature_add05": "0",
    }


def _with_funcs(**on) -> dict:
    status = _status(
        is_energy_conservation_avalable="1",
        is_health_avalable="1",
        is_indoor_unit_silent_avalable="1",
        is_self_clean_avalable="1",
    )
    status.update(energy_conservation="0", health="0", indoor_unit_silent="0", self_clean=0)
    status.update(on)
    return status


def test_temperature_change_cancels_energy_saving() -> None:
    status = _with_funcs(energy_conservation="1", health="1")
    assert build_command(status, {"temperature": 26.0}) == {
        "set_temp": 26,
        "set_temperature_add05": "0",
        # every available function is sent once any of them changes
        "energy_conservation": "0",
        "health": "1",
        "indoor_unit_silent": "0",
        "self_clean": 0,
    }


def test_fan_change_cancels_mute() -> None:
    status = _with_funcs(indoor_unit_silent="1", health="1")
    cmd = build_command(status, {"fan": "high"})
    assert cmd["wire_controller_wind_high"] == "1"
    assert cmd["indoor_unit_silent"] == "0"
    assert cmd["health"] == "1"


def test_mode_change_keeps_only_health() -> None:
    status = _with_funcs(energy_conservation="1", health="1", self_clean=1)
    cmd = build_command(status, {"mode": "heat"})
    assert cmd["energy_conservation"] == "0"
    assert cmd["self_clean"] == 0  # int-valued like the app
    assert cmd["health"] == "1"


def test_power_off_clears_functions() -> None:
    status = _with_funcs(health="1")
    cmd = build_command(status, {"power": False})
    assert cmd["if_run"] == "0"
    assert cmd["health"] == "0"


def test_functions_untouched_when_unaffected() -> None:
    status = _with_funcs(health="1")
    assert build_command(status, {"temperature": 26.0}) == {
        "set_temp": 26,
        "set_temperature_add05": "0",
    }


def test_mode_change_to_floor_heating_drops_health() -> None:
    # modeChanged keeps HEALTH unless the new mode is FLOOR.
    status = _with_funcs(health="1", is_floor_heating_avalable="1")
    cmd = build_command(status, {"mode": "floor_heat"})
    assert cmd["wire_controller_floor_heat"] == "1"
    assert cmd["health"] == "0"
    status = _with_funcs(health="1", energy_conservation="1", controllerFloorHeatFix="1")
    cmd = build_command(status, {"mode": "heat_floor_heat"})
    assert cmd["energy_conservation"] == "0"
    assert cmd["health"] == "1"  # only plain floor heating drops it


# ------------------------------------------------------------------ all modes

# The app's allModeArr (chunk 3): mode id -> control key, support key.
APP_MODES = [
    ("fresh_air_eco", "wire_controller_saveene_freshair", "saveene_freshair"),
    ("cool", "wire_controller_makecold", "wire_controller_makecold_fix"),
    ("heat", "wire_controller_heating", "wire_controller_heating_fix"),
    ("fan_only", "wire_controller_makewind", "wire_controller_makewind_fix"),
    ("dry", "wire_controller_rmwet", "wire_controller_rmwet_fix"),
    ("auto", "wire_controller_auto", "wire_controller_auto_fix"),
    ("dry_comfort", "wire_controller_dry_comfortable_rmwet", "is_dehumidification2_avalable"),
    ("dry_strong", "wire_controller_strong_rmwet", "is_dehumidification3_avalable"),
    ("dry_auto", "wire_controller_auto_rmwet", "wire_controller_auto_rmwet_fix"),
    ("floor_heat", "wire_controller_floor_heat", "is_floor_heating_avalable"),
    ("dry_floor_heat", "wire_controller_rmwet_floor_heating", "controllerFloorRmwetFix"),
    ("fan_floor_heat", "wire_controller_air_supply_floor_heating", "air_supply_floor_heating"),
    ("heat_floor_heat", "wire_controller_heat_floor_heating", "controllerFloorHeatFix"),
    ("purify", "wire_controller_purification", "is_purification_available"),
    ("heat_supply", "wire_controller_heating_mode", "is_heating_mode_available"),
]
LIMITS = {"cold_down_value": 18, "cold_up_value": 31, "heat_down_value": 17, "heat_up_value": 30}
COOL, HEAT = (18, 31), (17, 30)
# getTempLimit / refreshTempEnable / refreshWindDisplay, and the HVAC group.
EXPECTED = {
    "fresh_air_eco": ("fan_only", (1, 99), False, True),
    "cool": ("cool", COOL, True, True),
    "heat": ("heat", HEAT, True, True),
    "fan_only": ("fan_only", COOL, True, True),
    "dry": ("dry", COOL, True, True),
    "auto": ("auto", (16, 32), True, True),
    "dry_comfort": ("dry", COOL, True, True),
    "dry_strong": ("dry", COOL, True, True),
    "dry_auto": ("dry", COOL, False, True),
    "floor_heat": ("heat", HEAT, True, False),
    "dry_floor_heat": ("dry", HEAT, False, True),
    "fan_floor_heat": ("fan_only", HEAT, True, True),
    "heat_floor_heat": ("heat", HEAT, True, True),
    "purify": ("fan_only", HEAT, True, True),
    "heat_supply": ("heat", HEAT, True, True),
}


def _all_modes_off() -> dict:
    status = _status(**LIMITS)
    for _, control_key, support_key in APP_MODES:
        status[control_key] = "0"
        status[support_key] = "1"
    return status


def test_mode_table_matches_app() -> None:
    assert [(m, *keys) for m, keys in MODES.items()] == APP_MODES


def test_supported_modes_in_app_order() -> None:
    assert supported_modes(_all_modes_off()) == [m for m, _, _ in APP_MODES]
    assert supported_modes(_status()) == ["cool", "heat", "fan_only", "dry"]


@pytest.mark.parametrize(("mode", "control_key", "support_key"), APP_MODES)
def test_every_mode(mode: str, control_key: str, support_key: str) -> None:
    group, limits, temperature, fan = EXPECTED[mode]
    status = _all_modes_off()
    status[control_key] = "1"
    assert current_mode(status) == mode
    assert MODE_GROUP[mode] == group
    assert temp_limits(status, mode) == limits
    assert has_temperature(status | {"saveene_freshair": "0"}, mode) is temperature
    assert has_fan(mode) is fan
    # Selecting it sends its own control key.
    other = "cool" if mode != "cool" else "heat"
    assert build_command(status, {"mode": other})[MODES[other][0]] == "1"
    assert control_key in build_command(_all_modes_off() | {MODES[other][0]: "1"}, {"mode": mode})


# getHumiLimit (both services) and where the page shows the humidity slider
# instead of the temperature (refreshTempEnable / refreshHumiEnable).
HUMIDITY = {
    "dry_comfort": (35, 90),
    "dry_strong": (35, 90),
    "dry_auto": (35, 90),
    "dry_floor_heat": (35, 90),
}
HUMIDITY_SLIDER = {"dry_auto", "dry_floor_heat"}
# The standard page (yNUN) has 13 modes, and getTempLimit has no CLEAR case.
STANDARD_MISSING = {"fresh_air_eco", "heat_supply"}


@pytest.mark.parametrize(("mode", "control_key", "support_key"), APP_MODES)
@pytest.mark.parametrize("profile", [COLOR, STANDARD], ids=["color", "standard"])
def test_every_mode_both_pages(profile, mode: str, control_key: str, support_key: str) -> None:
    group, limits, temperature, fan = EXPECTED[mode]
    status = _all_modes_off()
    status[control_key] = "1"
    if profile is STANDARD and mode in STANDARD_MISSING:
        assert mode not in supported_modes(status, profile)
        assert current_mode(status, profile) is None
        assert mode not in STANDARD.modes
        return
    if profile is STANDARD and mode == "purify":
        limits = (1, 99)
    assert mode in supported_modes(status, profile)
    assert current_mode(status, profile) == mode
    assert temp_limits(status, mode, profile) == limits
    assert has_temperature(status | {"saveene_freshair": "0"}, mode, profile) is temperature
    assert has_fan(mode) is fan
    assert humidity_limits(mode) == HUMIDITY.get(mode, (35, 70))
    expected = ("mode", HUMIDITY[mode]) if mode in HUMIDITY_SLIDER else None
    # (Every support key is "1" here, which makes it a fresh-air controller.)
    assert humidity_control(status | {"saveene_freshair": "0"}, mode, profile) == expected
    # Written with its own control key, read back as that mode.
    other = "cool" if mode != "cool" else "heat"
    cmd = build_command(_all_modes_off() | {MODES[other][0]: "1"}, {"mode": mode}, profile)
    assert cmd[control_key] == "1"


def test_first_mode_flag_wins() -> None:
    # getMode: allModeArr.find(), so the app's order decides.
    status = _all_modes_off() | {
        "wire_controller_heating_mode": "1",
        "wire_controller_heating": "1",
    }
    assert current_mode(status) == "heat"


def test_fresh_air_controller_has_no_setpoint() -> None:
    status = _status(saveene_freshair="1")
    assert not has_temperature(status, "cool")
    assert has_temperature(_status(), "cool")


def test_mode_change_clamps_to_heat_range_for_floor_modes() -> None:
    status = _all_modes_off() | {"wire_controller_makecold": "1", "set_temp": 31}
    status["set_temperature_add05"] = "0"
    assert build_command(status, {"mode": "heat_floor_heat"}) == {
        "wire_controller_heat_floor_heating": "1",
        "set_temp": 30,
        "set_temperature_add05": "0",
    }


def test_one_direction_human_sense_closed_on_power_and_mode() -> None:
    # switchChanged / modeChanged -> closeOneHumanFeeling(), same request.
    status = _status(one_direction_human_sense="2")
    assert build_command(status, {"power": False}) == {
        "if_run": "0",
        "one_direction_human_sense": "0",
    }
    assert build_command(status, {"mode": "heat"}) == {
        "wire_controller_heating": "1",
        "one_direction_human_sense": "0",
    }
    # Not for temperature or fan changes, nor when it is already off.
    assert "one_direction_human_sense" not in build_command(status, {"temperature": 26.0})
    assert "one_direction_human_sense" not in build_command(status, {"fan": "high"})
    assert build_command(_status(one_direction_human_sense="0"), {"power": False}) == {
        "if_run": "0"
    }
    missing = {k: v for k, v in _status().items() if k != "one_direction_human_sense"}
    assert build_command(missing, {"power": False}) == {"if_run": "0"}
    assert build_command(_status(one_direction_human_sense=1), {"power": False}) == {
        "if_run": "0",
        "one_direction_human_sense": "0",
    }
