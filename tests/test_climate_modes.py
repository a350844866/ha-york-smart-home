"""Climate behavior for controllers with more than the user's four modes,
constants refreshed from the controller detail, and polling details."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re

import pytest

from custom_components.york_smart_home.api import YorkApiError, YorkConnectionError
from custom_components.york_smart_home.const import DOMAIN, MODES
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .conftest import CODE, FIXTURE
from .test_integration import _call, _last_ctrl, _setup

# Synthetic controller offering every colour-controller mode (the user's unit
# only has cool/heat/fan/dry), with distinct cooling and heating ranges.
EXTENDED_SUPPORT = {
    "saveene_freshair": "0",  # "1" would make it a fresh-air controller
    "wire_controller_auto_fix": "1",
    "is_dehumidification2_avalable": "1",
    "is_dehumidification3_avalable": "1",
    "wire_controller_auto_rmwet_fix": "1",
    "is_floor_heating_avalable": "1",
    "controllerFloorRmwetFix": "1",
    "air_supply_floor_heating": "1",
    "controllerFloorHeatFix": "1",
    "is_purification_available": "1",
    "is_heating_mode_available": "1",
    "cold_down_value": 18,
    "cold_up_value": 31,
    "heat_down_value": 17,
    "heat_up_value": 30,
}
MODE_KEYS = {
    "cool": "wire_controller_makecold",
    "heat": "wire_controller_heating",
    "floor_heat": "wire_controller_floor_heat",
    "heat_floor_heat": "wire_controller_heat_floor_heating",
    "dry_auto": "wire_controller_auto_rmwet",
    "fan_only": "wire_controller_makewind",
    "purify": "wire_controller_purification",
}


def _in_mode(mode: str, **extra) -> dict:
    status = {**FIXTURE["status"], "wire_controller_makecold": "0", **extra}
    status[MODE_KEYS[mode]] = "1"
    return status


def _use(mock_api, status: dict, **constants) -> None:
    mock_api.get_status.side_effect = lambda home, codes: {CODE: copy.deepcopy(status)}
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: {
        **FIXTURE["device"]["constantJson"],
        **EXTENDED_SUPPORT,
        **constants,
    }


def _features(state) -> ClimateEntityFeature:
    return ClimateEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])


# --------------------------------------------------------------- user device


async def test_user_device_has_no_presets(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert not _features(state) & ClimateEntityFeature.PRESET_MODE
    assert "preset_modes" not in state.attributes
    assert state.attributes["hvac_modes"] == ["off", "cool", "heat", "fan_only", "dry"]


async def test_fan_only_keeps_temperature(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The app still sets the temperature in fan mode, with the cooling range."""
    status = _in_mode("fan_only")
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.FAN_ONLY
    assert _features(state) & ClimateEntityFeature.TARGET_TEMPERATURE
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (19, 30)
    await _call(hass, SERVICE_SET_TEMPERATURE, entity_id, **{ATTR_TEMPERATURE: 25})
    assert _last_ctrl(mock_api) == {"set_temp": 25, "set_temperature_add05": "0"}


@pytest.mark.parametrize(
    ("flag", "step"),
    [("1", 0.5), (None, 0.5), ("0", 1.0), (0, 1.0)],
)
async def test_temperature_step(
    hass: HomeAssistant, mock_api, config_entry, flag, step: float
) -> None:
    """Wire controller pages hardcode 0.5; only an explicit "0" limits it."""
    constants = dict(FIXTURE["device"]["constantJson"])
    del constants["is_set_temperature05_avalable"]
    if flag is not None:
        constants["is_set_temperature05_avalable"] = flag
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: dict(constants)
    mock_api.get_wire_controllers.return_value[0]["constantJson"] = dict(constants)
    entity_id = await _setup(hass, config_entry)
    assert hass.states.get(entity_id).attributes["target_temp_step"] == step


# ------------------------------------------------------------ extended modes


async def test_extended_modes(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, _in_mode("floor_heat"))
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert state.attributes["hvac_modes"] == ["off", "cool", "heat", "fan_only", "dry", "auto"]
    assert state.attributes["preset_modes"] == [
        "cool",
        "heat",
        "fan_only",
        "dry",
        "auto",
        "dry_comfort",
        "dry_strong",
        "dry_auto",
        "floor_heat",
        "dry_floor_heat",
        "fan_floor_heat",
        "heat_floor_heat",
        "purify",
        "heat_supply",
    ]
    # Floor heating is shown as heat, with its own preset, heating range and no fan.
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_PRESET_MODE] == "floor_heat"
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (17, 30)
    features = _features(state)
    assert features & ClimateEntityFeature.PRESET_MODE
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert not features & ClimateEntityFeature.FAN_MODE
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_SET_FAN_MODE, entity_id, fan_mode="high")

    # Choosing the HVAC mode it already shows keeps the specific mode.
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    mock_api.control.assert_not_called()

    # A preset sends that mode's own key.
    await _call(hass, SERVICE_SET_PRESET_MODE, entity_id, **{ATTR_PRESET_MODE: "heat_supply"})
    assert _last_ctrl(mock_api) == {"wire_controller_heating_mode": "1"}
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_PRESET_MODE] == "heat_supply"

    # An HVAC mode of another group sends its basic mode.
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.DRY})
    assert _last_ctrl(mock_api)["wire_controller_rmwet"] == "1"


async def test_preset_turns_on(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, _in_mode("cool", if_run="0"))
    entity_id = await _setup(hass, config_entry)
    await _call(hass, SERVICE_SET_PRESET_MODE, entity_id, **{ATTR_PRESET_MODE: "dry_strong"})
    assert _last_ctrl(mock_api) == {"if_run": "1", "wire_controller_strong_rmwet": "1"}


async def test_preset_respects_mode_lock(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, _in_mode("cool"), run_model_fixed="1")
    entity_id = await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, SERVICE_SET_PRESET_MODE, entity_id, **{ATTR_PRESET_MODE: "purify"})
    assert info.value.translation_key == "mode_locked"
    mock_api.control.assert_not_called()


async def test_group_without_basic_mode(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A unit with floor heating but no plain heat: HEAT selects floor heating."""
    _use(mock_api, _in_mode("cool"), wire_controller_heating_fix="0", is_heating_mode_available="0")
    entity_id = await _setup(hass, config_entry)
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    # The first heat-group mode in the app's order.
    assert _last_ctrl(mock_api) == {"wire_controller_floor_heat": "1"}


async def test_mode_without_setpoint(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Auto dry sets humidity instead of temperature in the app."""
    _use(mock_api, _in_mode("dry_auto"))
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.DRY
    assert state.attributes[ATTR_PRESET_MODE] == "dry_auto"
    assert not _features(state) & ClimateEntityFeature.TARGET_TEMPERATURE
    assert state.attributes.get(ATTR_TEMPERATURE) is None
    assert _features(state) & ClimateEntityFeature.FAN_MODE


async def test_fresh_air_controller_hides_setpoint(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _use(mock_api, _in_mode("purify"), saveene_freshair="1")
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.FAN_ONLY
    assert state.attributes[ATTR_PRESET_MODE] == "purify"
    assert "fresh_air_eco" in state.attributes["preset_modes"]
    assert not _features(state) & ClimateEntityFeature.TARGET_TEMPERATURE


async def test_unmapped_mode_regression(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Before 0.5.0 a unit in floor heating had hvac_mode None."""
    _use(mock_api, _in_mode("heat_floor_heat"))
    entity_id = await _setup(hass, config_entry)
    assert hass.states.get(entity_id).state == HVACMode.HEAT


# ------------------------------------------------------------------ constants


async def test_constants_refreshed_on_discovery(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    entity_id = await _setup(hass, config_entry)
    assert not hass.states.get(entity_id).attributes["mode_locked"]
    mock_api.get_wire_controller_detail.assert_awaited_with(100001, CODE)

    # Locked on the wall panel; the app sees it the next time the page opens.
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: {
        **FIXTURE["device"]["constantJson"],
        "run_model_fixed": "1",
        "cold_down_value": 22,
    }
    coordinator = config_entry.runtime_data
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    state = hass.states.get(entity_id)
    assert state.attributes["mode_locked"]
    assert state.attributes["min_temp"] == 22
    # Live values are untouched.
    assert state.attributes["temperature"] == 27.5
    assert state.state == HVACMode.COOL


async def test_constants_win_over_status(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Like the app, capability/lock keys come from the constants when present."""
    status = {**FIXTURE["status"], "run_model_fixed": "1", "wire_controller_auto_fix": "1"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert not state.attributes["mode_locked"]
    assert "auto" not in state.attributes["hvac_modes"]


@pytest.mark.parametrize(
    "detail",
    [YorkConnectionError("down"), YorkApiError("xkq detail: invalid response"), {}],
)
async def test_constants_fallback_to_device_list(
    hass: HomeAssistant, mock_api, config_entry, detail
) -> None:
    if isinstance(detail, Exception):
        mock_api.get_wire_controller_detail.side_effect = detail
    else:
        mock_api.get_wire_controller_detail.side_effect = None
        mock_api.get_wire_controller_detail.return_value = detail
    device = copy.deepcopy(FIXTURE["device"])
    device["constantJson"]["B9"] = "1"
    mock_api.get_wire_controllers.return_value = [device]
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.COOL
    assert state.attributes["fan_locked"]
    assert state.attributes["hvac_modes"] == ["off", "cool", "heat", "fan_only", "dry"]


async def test_falsy_constants_ignored(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The app copies a constant only if it is truthy in JavaScript."""
    status = {**FIXTURE["status"], "run_model_fixed": "1", "B9": "1", "set_temp_fixed": "1"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: {
        **FIXTURE["device"]["constantJson"],
        "run_model_fixed": None,
        "B9": "",
        "set_temp_fixed": "0",  # the string "0" is truthy: it wins
    }
    entity_id = await _setup(hass, config_entry)
    attrs = hass.states.get(entity_id).attributes
    assert attrs["mode_locked"] and attrs["fan_locked"]
    assert not attrs["temperature_locked"]


async def test_constants_reach_commands(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Commands are built from the constants too (limits, available functions)."""
    status = {
        **FIXTURE["status"],
        "set_temp": 30,
        "set_temperature_add05": "0",
        "energy_conservation": "1",
    }
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: {
        **FIXTURE["device"]["constantJson"],
        "heat_up_value": 26,
        "is_energy_conservation_avalable": "1",
    }
    await _setup(hass, config_entry)
    await config_entry.runtime_data.async_request_change(CODE, mode="heat")
    ctrl = _last_ctrl(mock_api)
    assert ctrl["wire_controller_heating"] == "1"
    assert ctrl["set_temp"] == 26  # clamped to the heating range from the constants
    assert ctrl["energy_conservation"] == "0"  # cancelled by the mode change


async def test_diagnostics_extended_constants(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, _in_mode("purify"), horizontal_air_deflector_num=2)
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    constants = diag["devices"][0]["constants"]
    assert constants["is_purification_available"] == "1"
    assert constants["air_supply_floor_heating"] == "1"
    assert constants["controllerFloorRmwetFix"] == "1"
    assert constants["horizontal_air_deflector_num"] == 2
    assert constants["heat_up_value"] == 30


# -------------------------------------------------------------------- polling


async def test_round_time_cursor(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The app sends roundTime back as the cursor (as a number)."""
    ts = FIXTURE["status"]["timestamp"]
    report = {**FIXTURE["status"], "roundTime": str(ts + 777)}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(report)}
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, ts + 777)]

    # Older by roundTime: ignored, even with a newer timestamp.
    stale = {**FIXTURE["status"], "roundTime": ts, "timestamp": ts + 5000, "if_run": "0"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(stale)}
    await coordinator.async_refresh()
    assert coordinator.devices[CODE].status["if_run"] == "1"

    # Without roundTime the timestamp is used.
    newer = {k: v for k, v in FIXTURE["status"].items() if k != "roundTime"}
    newer["timestamp"] = ts + 9000
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(newer)}
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, ts + 9000)]


# --------------------------------------------------------------- translations


def _keys(tree: dict, prefix: str = "") -> set[str]:
    keys = set()
    for key, value in tree.items():
        path = f"{prefix}.{key}"
        keys |= _keys(value, path) if isinstance(value, dict) else {path}
    return keys


def test_translations_in_sync() -> None:
    base = Path(__file__).parent.parent / "custom_components" / DOMAIN
    strings = json.loads((base / "strings.json").read_text("utf-8"))
    english = json.loads((base / "translations" / "en.json").read_text("utf-8"))
    chinese = json.loads((base / "translations" / "zh-Hans.json").read_text("utf-8"))
    assert strings == english
    assert _keys(strings) == _keys(chinese)
    fan = strings["entity"]["climate"]["york"]["state_attributes"]["fan_mode"]["state"]
    assert fan["silent"] == "Breeze"  # 微风, not the "mute" function


def test_translations_cover_code() -> None:
    base = Path(__file__).parent.parent / "custom_components" / DOMAIN
    strings = json.loads((base / "strings.json").read_text("utf-8"))
    presets = strings["entity"]["climate"]["york"]["state_attributes"]["preset_mode"]["state"]
    assert set(MODES) == set(presets)
    used = set()
    for source in base.glob("*.py"):
        used |= set(
            re.findall(
                r'translation_domain=DOMAIN,\s*translation_key="(\w+)"', source.read_text("utf-8")
            )
        )
    assert used
    assert used <= set(strings["exceptions"])
