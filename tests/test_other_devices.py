"""Entities for controllers with more capabilities than the user's unit.

Synthetic controllers built from the user's real record: extra functions,
humidity, air deflectors, human sensing, a fresh-air colour controller and
the standard controller (xkqType 8).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import time
from unittest.mock import patch

import pytest

from custom_components.york_smart_home.api import YorkSessionTakenError
from custom_components.york_smart_home.const import DOMAIN, FUNC_LABELS
from custom_components.york_smart_home.control import (
    filter_due,
    filter_reset_state,
    fresh_air_filters,
)
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.profile import COLOR, STANDARD, SWITCH_FUNCS
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate import (
    ATTR_HUMIDITY,
    ATTR_SWING_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HUMIDITY,
    SERVICE_SET_SWING_MODE,
    ClimateEntityFeature,
)
from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import CODE, FIXTURE

BASE = Path(__file__).parent.parent / "custom_components" / DOMAIN


def _use(mock_api, *, status=None, constants=None, device=None) -> None:
    """Serve a synthetic controller: the fixture with these overrides."""
    full_status = {**FIXTURE["status"], **(status or {})}
    full_constants = {**FIXTURE["device"]["constantJson"], **(constants or {})}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: copy.deepcopy(full_status)}
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: copy.deepcopy(
        full_constants
    )
    if device:
        mock_api.get_wire_controllers.return_value = [{**FIXTURE["device"], **device}]


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass, platform: str, key: str | None) -> str | None:
    unique_id = f"{CODE}_{key}" if key else CODE
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def _entities(hass, platform: str) -> set[str]:
    """Unique id suffixes of this controller's entities on a platform."""
    return {
        entry.unique_id.removeprefix(f"{CODE}_")
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN and entry.domain == platform
    }


async def _service(hass, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


def _last_ctrl(mock_api) -> dict:
    return mock_api.control.call_args.args[4]


def _later(seconds: float = 60):
    return patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + seconds,
    )


def _features(hass) -> ClimateEntityFeature:
    state = hass.states.get(_entity(hass, CLIMATE_DOMAIN, None))
    return ClimateEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])


# ---------------------------------------------------------------- user unit


async def test_user_device_gets_no_new_entities(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    assert _entities(hass, SWITCH_DOMAIN) == {"key_lock", "key_sound_switch", "auto_brightness"}
    assert _entities(hass, SELECT_DOMAIN) == {"simple_timer"}
    assert _entities(hass, SENSOR_DOMAIN) == {
        "indoor_temperature",
        "running_state",
        "fault_code",
        "timer_end",
    }
    state = hass.states.get(_entity(hass, CLIMATE_DOMAIN, None))
    assert "swing_modes" not in state.attributes
    assert state.attributes.get("current_humidity") is None
    features = _features(hass)
    assert not features & ClimateEntityFeature.SWING_MODE
    assert not features & ClimateEntityFeature.TARGET_HUMIDITY


# ----------------------------------------------------------- extra functions

ALL_FUNCTIONS = {
    "is_energy_conservation_avalable": "1",
    "is_sleep_avalable": "1",
    "is_indoor_unit_silent_avalable": "1",
    "is_health_avalable": "1",
    "is_forest_wind_avalable": "1",
    "is_auxiliary_heat_avalable": "1",
    "is_self_clean_avalable": "1",
    "is_force_avalable": "1",
    "is_nosense_wind_avalable": "1",
    "is_awayhome_saveenergy_avalable": "1",
    "hig_temperature_sterilization_fix": "1",
    "is_humidification_avalable": "1",
    "human_sense_type2": "2",  # 3D sensor: smart sensing, 风吹人 / 风避人
}


async def test_function_switches(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, constants=ALL_FUNCTIONS, status={"energy_conservation": "1"})
    await _setup(hass, config_entry)
    assert _entities(hass, SWITCH_DOMAIN) >= set(SWITCH_FUNCS)
    save = _entity(hass, SWITCH_DOMAIN, "save")
    sleep = _entity(hass, SWITCH_DOMAIN, "sleep")
    assert hass.states.get(save).state == "on"
    assert hass.states.get(save).attributes["changeable"] is True
    assert hass.states.get(sleep).state == "off"
    # Names come from the translations (节能 -> "Eco").
    assert hass.states.get(save).name.endswith("Eco")

    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, sleep)
    cmd = _last_ctrl(mock_api)
    assert cmd["sleep"] == "1" and cmd["energy_conservation"] == "0"
    assert cmd["self_clean"] == 0 and cmd["hig_temperature_sterilization"] == 0
    assert "one_direction_human_sense" not in cmd  # not offered via funcs
    assert hass.states.get(sleep).state == "on"
    assert hass.states.get(save).state == "off"

    # 辅热 only in heating: refused, the switch stays available.
    aux = _entity(hass, SWITCH_DOMAIN, "aux_heat")
    assert hass.states.get(aux).attributes["changeable"] is False
    mock_api.control.reset_mock()
    with pytest.raises(ServiceValidationError) as err:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, aux)
    assert err.value.translation_key == "function_not_available"
    # The placeholder names the function, not its internal id.
    assert err.value.translation_placeholders == {"function": "Auxiliary heating"}
    hass.config.language = "zh-Hans"
    with pytest.raises(ServiceValidationError) as err:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, aux)
    assert err.value.translation_placeholders == {"function": "辅热"}
    mock_api.control.assert_not_called()
    assert hass.states.get(aux).state == "off"


@pytest.mark.parametrize("how", ["offline", "ota"])
async def test_function_switch_refused_offline_or_updating(
    hass: HomeAssistant, mock_api, config_entry, how: str
) -> None:
    status = {"otaFlg": "1"} if how == "ota" else None
    _use(mock_api, constants=ALL_FUNCTIONS, status=status)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    if how == "offline":
        mock_api.get_online.return_value = {}  # missing counts as offline
        coordinator._last_online = None
        await coordinator.async_refresh()
    mock_api.control.reset_mock()
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_request_change(CODE, funcs={"sleep": True})
    assert err.value.translation_key == ("offline" if how == "offline" else "ota_in_progress")
    mock_api.control.assert_not_called()


async def test_unsupported_change_does_not_reclaim_session(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A change the controller can never accept is refused without logging
    the phone app out."""
    await _setup(hass, config_entry)  # the user's unit: no extra functions
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    assert coordinator.session_yielded
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_request_change(CODE, funcs={"sleep": True})
    assert err.value.translation_key == "function_not_supported"
    assert err.value.translation_placeholders == {"function": "Sleep"}
    mock_api.relogin.assert_not_called()
    assert coordinator.session_yielded
    mock_api.control.assert_not_called()


async def test_state_dependent_refusal_reclaims_session(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Whether a function can be toggled now depends on the live state, which
    may have changed on the app: decided after taking the session back."""
    _use(mock_api, constants=ALL_FUNCTIONS)  # cooling: 辅热 not available
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    assert coordinator.session_yielded
    # Meanwhile the unit was switched to heat on the app.
    _use(
        mock_api,
        constants=ALL_FUNCTIONS,
        status={"wire_controller_makecold": "0", "wire_controller_heating": "1"},
    )
    await coordinator.async_request_change(CODE, funcs={"aux_heat": True})
    mock_api.relogin.assert_awaited_once()
    assert _last_ctrl(mock_api)["auxiliary_heat"] == "1"


def test_function_labels_match_entity_names() -> None:
    """The error placeholder uses the names the entities have."""
    names = {}
    for lang, path in ((1, BASE / "strings.json"), (0, BASE / "translations" / "zh-Hans.json")):
        entity = json.loads(path.read_text("utf-8"))["entity"]
        switches = entity["switch"]
        sensing = entity["select"]["human_sensing"]
        for func in SWITCH_FUNCS:
            names[func, lang] = switches[func]["name"]
        names["people", lang] = switches["independent_human_sense"]["name"]
        names["people_one", lang] = sensing["name"]
        names["human_sensing", lang] = sensing["name"]
        names["wind_blow", lang] = sensing["state"]["blow"]
        names["wind_shelter", lang] = sensing["state"]["avoid"]
    assert {(func, lang): FUNC_LABELS[func][lang] for func, lang in names} == names
    assert {func for func, _ in names} == set(FUNC_LABELS)


async def test_self_clean_switch(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, constants=ALL_FUNCTIONS, status={"if_run": "0"})
    await _setup(hass, config_entry)
    clean = _entity(hass, SWITCH_DOMAIN, "clean")
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, clean)
    cmd = _last_ctrl(mock_api)
    assert cmd["if_run"] == "1" and cmd["self_clean"] == 1
    assert hass.states.get(clean).state == "on"
    # The overlay's stop button: self_clean 0 and the unit off.
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, clean)
    assert _last_ctrl(mock_api) == {"self_clean": 0, "if_run": "0"}


# ----------------------------------------------------------------- humidity


async def test_humidity_in_auto_dehumidification(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _use(
        mock_api,
        constants={"wire_controller_auto_rmwet_fix": "1"},
        status={"wire_controller_makecold": "0", "wire_controller_auto_rmwet": "1"},
    )
    await _setup(hass, config_entry)
    climate = _entity(hass, CLIMATE_DOMAIN, None)
    state = hass.states.get(climate)
    features = _features(hass)
    assert features & ClimateEntityFeature.TARGET_HUMIDITY
    assert not features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert state.attributes["humidity"] == 65
    assert state.attributes["min_humidity"] == 35
    assert state.attributes["max_humidity"] == 90
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HUMIDITY,
        {ATTR_ENTITY_ID: climate, ATTR_HUMIDITY: 50},
        blocking=True,
    )
    assert _last_ctrl(mock_api) == {"set_humidity": 50}
    assert hass.states.get(climate).attributes["humidity"] == 50


async def test_humidification_target(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        constants={"is_humidification_avalable": "1"},
        status={"humidification": "1", "set_humidity": 45},
    )
    await _setup(hass, config_entry)
    climate = _entity(hass, CLIMATE_DOMAIN, None)
    state = hass.states.get(climate)
    assert _features(hass) & ClimateEntityFeature.TARGET_HUMIDITY
    assert (state.attributes["min_humidity"], state.attributes["max_humidity"]) == (30, 60)
    assert state.attributes["humidity"] == 45
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HUMIDITY,
        {ATTR_ENTITY_ID: climate, ATTR_HUMIDITY: 55},
        blocking=True,
    )
    assert _last_ctrl(mock_api) == {"set_humidity": 55}
    # Humidification off: no target humidity in cooling.
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, _entity(hass, SWITCH_DOMAIN, "humidify"))
    assert _last_ctrl(mock_api) == {"humidification": "0"}
    assert not _features(hass) & ClimateEntityFeature.TARGET_HUMIDITY


# ------------------------------------------------------------ air deflectors

DEFLECTORS = {
    "horizontal_air_deflector_num": "2",
    "vertical_air_deflector_num": "1",
    "is_air_deflector_avalable": "1",
    "air_deflector_independent_or_together_ctrl": "1",
}
LEVELS = {
    "position_of_air_deflector1": "0",
    "section_of_air_deflector1": "2",
    "position_of_air_deflector2": "1",
    "position_of_air_deflector3": "0",
    "section_of_air_deflector3": "6",
}


async def test_deflectors(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, constants=DEFLECTORS, status=LEVELS)
    await _setup(hass, config_entry)
    climate = _entity(hass, CLIMATE_DOMAIN, None)
    state = hass.states.get(climate)
    assert _features(hass) & ClimateEntityFeature.SWING_MODE
    assert state.attributes["swing_modes"] == ["auto", *map(str, range(1, 8))]
    assert state.attributes["swing_mode"] == "3"
    assert _entities(hass, SELECT_DOMAIN) >= {"air_deflector2", "air_deflector3"}
    second = _entity(hass, SELECT_DOMAIN, "air_deflector2")
    assert hass.states.get(second).state == "auto"
    assert hass.states.get(_entity(hass, SELECT_DOMAIN, "air_deflector3")).state == "7"
    assert hass.states.get(second).name.endswith("Air deflector 2")

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_SWING_MODE,
        {ATTR_ENTITY_ID: climate, ATTR_SWING_MODE: "auto"},
        blocking=True,
    )
    assert _last_ctrl(mock_api) == {
        "position_of_air_deflector1": "1",
        "air_deflector1_independent_or_together_ctrl": "0",
        "position_of_air_deflector2": "1",
        "air_deflector2_independent_or_together_ctrl": "0",
        "position_of_air_deflector3": "0",
        "section_of_air_deflector3": "6",
        "air_deflector3_independent_or_together_ctrl": "0",
    }
    assert hass.states.get(climate).attributes["swing_mode"] == "auto"
    await _service(hass, SELECT_DOMAIN, SERVICE_SELECT_OPTION, second, **{ATTR_OPTION: "5"})
    cmd = _last_ctrl(mock_api)
    assert cmd["section_of_air_deflector2"] == "4"
    assert cmd["position_of_air_deflector1"] == "1"  # kept from the optimistic state


async def test_deflectors_hidden_in_floor_heating(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _use(
        mock_api,
        constants={**DEFLECTORS, "is_floor_heating_avalable": "1"},
        status={**LEVELS, "wire_controller_makecold": "0", "wire_controller_floor_heat": "1"},
    )
    await _setup(hass, config_entry)
    assert not _features(hass) & ClimateEntityFeature.SWING_MODE
    with pytest.raises(ServiceValidationError) as err:
        await _service(
            hass,
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            _entity(hass, SELECT_DOMAIN, "air_deflector2"),
            **{ATTR_OPTION: "1"},
        )
    assert err.value.translation_key == "swing_not_available"


async def test_unified_deflectors(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        constants={**DEFLECTORS, "is_air_deflector_avalable": "0"},
        status=LEVELS,
    )
    await _setup(hass, config_entry)
    # Unified control only: the climate swing sets every deflector.
    assert "air_deflector2" not in _entities(hass, SELECT_DOMAIN)
    climate = _entity(hass, CLIMATE_DOMAIN, None)
    assert hass.states.get(climate).attributes["swing_modes"][-1] == "5"  # cooling
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_SWING_MODE,
        {ATTR_ENTITY_ID: climate, ATTR_SWING_MODE: "2"},
        blocking=True,
    )
    cmd = _last_ctrl(mock_api)
    assert all(cmd[f"section_of_air_deflector{n}"] == "1" for n in (1, 2, 3))
    assert all(cmd[f"air_deflector{n}_independent_or_together_ctrl"] == "1" for n in (1, 2, 3))


async def test_3d_and_far_wind(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        constants={"is_3D_wind_avalable": "1", "is_super_far_wind_avalable": "1"},
        status={"three_air_supply": "1", "position_of_air_deflector2": "1"},
    )
    await _setup(hass, config_entry)
    climate = _entity(hass, CLIMATE_DOMAIN, None)
    state = hass.states.get(climate)
    assert state.attributes["swing_modes"][-2:] == ["three_d", "far"]
    assert state.attributes["swing_mode"] == "three_d"
    assert _entities(hass, SELECT_DOMAIN) >= {"air_deflector2"}
    # The app's 左右风; unique_id unchanged.
    horizontal = hass.states.get(_entity(hass, SELECT_DOMAIN, "air_deflector2"))
    assert horizontal.name.endswith("Left/right air")
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_SWING_MODE,
        {ATTR_ENTITY_ID: climate, ATTR_SWING_MODE: "far"},
        blocking=True,
    )
    assert _last_ctrl(mock_api) == {"ultra_far_air_supply": "1"}
    # 3D is deselected locally (the app never sends its "0") ...
    assert hass.states.get(climate).attributes["swing_mode"] == "far"
    coordinator = config_entry.runtime_data
    dev = coordinator.devices[CODE]
    assert dev.recheck_after is not None
    # ... and the controller's full state decides after the grace period.
    with _later():
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
    assert hass.states.get(climate).attributes["swing_mode"] == "three_d"


# ------------------------------------------------------------ human sensing


async def test_one_direction_human_sensing(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, constants={"human_sense_type": "1"})
    await _setup(hass, config_entry)
    select = _entity(hass, SELECT_DOMAIN, "human_sensing")
    state = hass.states.get(select)
    assert state.state == "off"
    assert state.attributes["options"] == ["off", "blow", "avoid", "unmanned"]
    await _service(hass, SELECT_DOMAIN, SERVICE_SELECT_OPTION, select, **{ATTR_OPTION: "avoid"})
    assert _last_ctrl(mock_api) == {"one_direction_human_sense": "2"}
    assert hass.states.get(select).state == "avoid"
    # Turning the unit off closes it in the same request (closeOneHumanFeeling).
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "turn_off",
        {ATTR_ENTITY_ID: _entity(hass, CLIMATE_DOMAIN, None)},
        blocking=True,
    )
    assert _last_ctrl(mock_api) == {"if_run": "0", "one_direction_human_sense": "0"}


async def test_k3_human_sensing(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(mock_api, constants={"human_sense_type": "2"})
    await _setup(hass, config_entry)
    switch = _entity(hass, SWITCH_DOMAIN, "independent_human_sense")
    action = _entity(hass, SELECT_DOMAIN, "human_sense_action")
    wait = _entity(hass, SELECT_DOMAIN, "human_sense_nobody_time")
    assert hass.states.get(switch).state == "off"
    assert hass.states.get(action).state == "keep_running"
    assert hass.states.get(wait).state == "60min"
    registry = er.async_get(hass)
    for entity_id in (action, wait):
        assert registry.async_get(entity_id).entity_category == EntityCategory.CONFIG
    await _service(hass, SELECT_DOMAIN, SERVICE_SELECT_OPTION, action, **{ATTR_OPTION: "stop"})
    assert _last_ctrl(mock_api) == {
        "independent_human_sense_effect": "0",
        "independent_human_sense_state": "3",
        "independent_human_sense_nobody_judge_time": "1",
    }
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, switch)
    assert _last_ctrl(mock_api) == {
        "independent_human_sense_effect": "1",
        "independent_human_sense_state": "3",
        "independent_human_sense_nobody_judge_time": "1",
    }
    assert hass.states.get(switch).state == "on"


# -------------------------------------------------------- fresh-air controller

AIR = {"indoor_humidity": 55, "indoor_pm25": 35, "indoor_co2": 820, "indoor_tvoc": 1}


async def test_fresh_air_controller(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        constants={"saveene_freshair": "1"},
        status={**AIR, "filter_life_tips3": 3, "filter_life_tips4": 2},
    )
    await _setup(hass, config_entry)
    for key, value in AIR.items():
        assert hass.states.get(_entity(hass, SENSOR_DOMAIN, key)).state == str(value), key
    pm25 = hass.states.get(_entity(hass, SENSOR_DOMAIN, "indoor_pm25"))
    # (Newer HA versions write the unit with the Greek mu.)
    assert pm25.attributes["unit_of_measurement"] in ("µg/m³", "μg/m³")
    assert (
        hass.states.get(_entity(hass, SENSOR_DOMAIN, "indoor_co2")).attributes[
            "unit_of_measurement"
        ]
        == "ppm"
    )
    assert (
        "unit_of_measurement"
        not in hass.states.get(_entity(hass, SENSOR_DOMAIN, "indoor_tvoc")).attributes
    )
    climate = hass.states.get(_entity(hass, CLIMATE_DOMAIN, None))
    assert climate.attributes["current_humidity"] == 55
    assert not _features(hass) & ClimateEntityFeature.TARGET_TEMPERATURE
    reminder = hass.states.get(_entity(hass, BINARY_SENSOR_DOMAIN, "filter"))
    assert reminder.state == "on"
    assert reminder.attributes["filters"] == {
        "primary": "ok",
        "humidification": "ok",
        "activated_carbon": "replace",
        "fresh_air_pm25": "replace",
        "return_air_pm25": "ok",
    }


async def test_fresh_air_values_hidden_while_off(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _use(
        mock_api,
        constants={"saveene_freshair": "1"},
        status={**AIR, "if_run": "0", "indoor_co2": 0},
    )
    await _setup(hass, config_entry)
    # All show "--" (unknown) while off; CO2 0 is not a reading either.
    assert hass.states.get(_entity(hass, SENSOR_DOMAIN, "indoor_co2")).state == "unknown"
    assert hass.states.get(_entity(hass, SENSOR_DOMAIN, "indoor_pm25")).state == "unknown"


async def test_fresh_air_value_becoming_plausible(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A sensor reading implausibly at start-up exists and shows up later."""
    _use(mock_api, constants={"saveene_freshair": "1"}, status={**AIR, "indoor_co2": 0})
    await _setup(hass, config_entry)
    co2 = _entity(hass, SENSOR_DOMAIN, "indoor_co2")
    assert hass.states.get(co2).state == "unknown"
    _use(mock_api, constants={"saveene_freshair": "1"}, status={**AIR, "indoor_co2": 800})
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(co2).state == "800"


def test_fresh_air_filter_states() -> None:
    assert fresh_air_filters(
        {
            "filter_life_tips1": 2,
            "filter_life_tips2": "x",  # not a number: left out
            "filter_life_tips3": 2,
            "filter_life_tips4": "0",
        }
    ) == {"primary": "clean", "activated_carbon": "clean", "fresh_air_pm25": "ok"}
    assert fresh_air_filters({"filter_life_tips1": 3, "filter_life_tips3": 3}) == {
        "primary": "ok",
        "activated_carbon": "replace",
    }


def test_standard_filter_rule() -> None:
    """xkq-control: only filter_life_tips1 == 2 raises the reminder."""
    assert filter_due({"filter_life_tips1": 2}, STANDARD) is True
    assert filter_due({"filter_life_tips3": 3}, STANDARD) is None  # slot 1 not reported
    assert filter_due({"filter_life_tips1": 0, "filter_life_tips3": 3}, STANDARD) is False
    assert filter_due({"filter_life_tips1": 0, "filter_life_tips3": 3}, COLOR) is True
    status = {"filter_life_tips1": 2, "filter_life_tips2": 2, "filter_life_tips3": 3}
    assert filter_reset_state(status, STANDARD) == {"filter_life_tips1": 0}
    assert filter_reset_state(status, COLOR) == {
        "filter_life_tips1": 0,
        "filter_life_tips2": 0,
        "filter_life_tips3": 0,
    }


# ---------------------------------------------------------- standard (xkqType 8)

STANDARD_DEVICE = {"xkqType": "8"}


async def test_standard_controller(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        device=STANDARD_DEVICE,
        constants={
            "is_energy_conservation_avalable": "2",  # anything but "0"
            "hig_temperature_sterilization_fix": "1",
            "is_heating_mode_available": "1",
            "human_sense_type": "1",
        },
        status={"filter_life_tips3": 3, "antifrozen_status": "1"},
    )
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.devices[CODE].profile.key == "standard"
    # Standard page banners: antifrozen_status, no defrost etc.
    banners = _entities(hass, BINARY_SENSOR_DOMAIN)
    assert "antifrozen_status" in banners
    assert "defrost_display" not in banners and "freeze_protection" not in banners
    freeze = er.async_get(hass).async_get(_entity(hass, BINARY_SENSOR_DOMAIN, "antifrozen_status"))
    assert freeze.disabled_by is not None
    # Filter: only filter_life_tips1 == 2 counts.
    assert hass.states.get(_entity(hass, BINARY_SENSOR_DOMAIN, "filter")).state == "off"
    # 供热 is not a standard-page mode.
    climate = hass.states.get(_entity(hass, CLIMATE_DOMAIN, None))
    assert "heat_supply" not in (climate.attributes.get("preset_modes") or [])
    # Functions: offered unless "0"; the K3 sensing from human_sense_type.
    assert {"save", "sterilize", "independent_human_sense"} <= _entities(hass, SWITCH_DOMAIN)
    assert "human_sensing" not in _entities(hass, SELECT_DOMAIN)
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "save"))
    assert _last_ctrl(mock_api) == {
        "energy_conservation": "1",
        "independent_human_sense_effect": "0",
        "hig_temperature_sterilization": "0",
    }
    assert mock_api.control.call_args.args[3] == "8"


async def test_standard_filter_reset(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        device=STANDARD_DEVICE,
        status={"filter_life_tips1": 2, "filter_life_tips3": 3},
    )
    await _setup(hass, config_entry)
    reminder = _entity(hass, BINARY_SENSOR_DOMAIN, "filter")
    assert hass.states.get(reminder).state == "on"
    await _service(hass, BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"))
    assert _last_ctrl(mock_api) == {"screen_mark_reset": "1"}
    assert hass.states.get(reminder).state == "off"
    view = config_entry.runtime_data.devices[CODE].view
    assert view["filter_life_tips1"] == 0 and view["filter_life_tips3"] == 3


# ------------------------------------------------------------- diagnostics


async def test_diagnostics_include_new_keys(hass: HomeAssistant, mock_api, config_entry) -> None:
    _use(
        mock_api,
        constants={
            **DEFLECTORS,
            "saveene_freshair": "1",
            "human_sense_type": "2",
            "B9": "0",
            "has_smart_people_feeling": "1",
        },
        status={**LEVELS, **AIR},
    )
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    device = diag["devices"][0]
    for key in (
        "position_of_air_deflector1",
        "section_of_air_deflector3",
        "air_deflector1_independent_or_together_ctrl",
        "set_humidity",
        "indoor_pm25",
        "indoor_tvoc",
        "independent_human_sense_state",
        "energy_conservation",
        "self_clean",
        "three_air_supply",
    ):
        assert key in device["status"], key
    for key in (
        "horizontal_air_deflector_num",
        "air_deflector_independent_or_together_ctrl",
        "is_air_deflector_avalable",
        "human_sense_type",
        "saveene_freshair",
        "B9",
        "has_smart_people_feeling",
    ):
        assert key in device["constants"], key
    assert CODE not in json.dumps(diag)


# ------------------------------------------------------------- translations


def test_refusal_keys_translated() -> None:
    strings = json.loads((BASE / "strings.json").read_text("utf-8"))
    used = set()
    for source in BASE.glob("*.py"):
        used |= set(re.findall(r'CommandRefused\(\s*"(\w+)"', source.read_text("utf-8")))
    assert used >= {"function_not_available", "swing_not_available", "humidity_not_supported"}
    assert used <= set(strings["exceptions"])


def test_new_entities_translated() -> None:
    strings = json.loads((BASE / "strings.json").read_text("utf-8"))
    entity = strings["entity"]
    assert set(SWITCH_FUNCS) <= set(entity["switch"])
    swing = entity["climate"]["york"]["state_attributes"]["swing_mode"]["state"]
    assert set(swing) == {"auto", *map(str, range(1, 8)), "three_d", "far"}
    deflector = entity["select"]["air_deflector"]
    assert "{number}" in deflector["name"]
    assert set(deflector["state"]) == {"auto", *map(str, range(1, 8))}
    assert entity["select"]["horizontal_air"]["state"] == deflector["state"]
    assert set(entity["select"]["human_sensing"]["state"]) == {"off", "blow", "avoid", "unmanned"}
    for key in ("indoor_humidity", "indoor_pm25", "indoor_co2", "indoor_tvoc"):
        assert key in entity["sensor"]
    assert "antifrozen_status" in entity["binary_sensor"]
