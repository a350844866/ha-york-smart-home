"""Read-only running information: status banners, running state, fault codes."""

from __future__ import annotations

import copy

import pytest

from custom_components.york_smart_home.api import YorkSessionTakenError
from custom_components.york_smart_home.const import DOMAIN, RUNNING_STATES, STATUS_BANNERS
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.status import (
    alarms,
    banner_on,
    cooling_down,
    has_banner,
    raw_alarm,
    running_state,
)
from custom_components.york_smart_home.trouble_codes import (
    APP_ALARM_TEXTS,
    TROUBLE_CODES,
    describe_code,
)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import CODE, FIXTURE

BANNERS_ON_BY_DEFAULT = {
    "defrost_display",
    "pre_heat_display",
    "mode_confict",
    "central_controller",
}


def _with_status(mock_api, status: dict) -> None:
    mock_api.get_status.side_effect = lambda home, codes: {CODE: copy.deepcopy(status)}


def _constants(mock_api, **overrides) -> None:
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: {
        **copy.deepcopy(FIXTURE["device"]["constantJson"]),
        **overrides,
    }


async def _setup(hass: HomeAssistant, entry, *, enable: tuple[str, ...] = ()) -> None:
    """Set up; ``enable`` pre-registers disabled-by-default binary sensors as enabled."""
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    for key in enable:
        ent_reg.async_get_or_create("binary_sensor", DOMAIN, f"{CODE}_{key}", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{CODE}_{key}")


def _state(hass, platform: str, key: str):
    return hass.states.get(_entity(hass, platform, key))


# --------------------------------------------------------------- fault codes


def test_trouble_code_table() -> None:
    # The whole table of the app's assets/data/trouble-code.json.
    assert len(TROUBLE_CODES) == 88
    assert all(code == code.upper() and len(code) == 2 for code in TROUBLE_CODES)
    assert TROUBLE_CODES["01"][0].startswith("室内机保护装置动作")


def test_describe_code() -> None:
    # Mixed case in the app's file ("0b", "bF"): looked up case-insensitively.
    assert describe_code("0B") == describe_code("0b") == {"description": TROUBLE_CODES["0B"][0]}
    assert describe_code("bf") == {"description": TROUBLE_CODES["BF"][0]}
    # getAlarmInfo's own texts win over the table (33 is not in it at all).
    assert describe_code("03") == {"description": APP_ALARM_TEXTS["03"]}
    assert describe_code("05")["description"].startswith("电源逆向或缺相")
    assert describe_code("33")["description"].startswith("加湿器故障")
    # Second reasons where the table has them.
    assert describe_code("60") == {
        "description": "集控器-室外机间通信异常(该系统有1台以上的室内机运行时)",
        "reason_two": "室内机开机时，该系统室外机离线",
    }
    assert describe_code("FE") == {}


def test_alarms_decoding() -> None:
    status = {
        **FIXTURE["status"],
        "iu1_alarm_code": "11",  # decimal 11 -> "0B" (lower case "0b" in the table)
        "iu2_alarm_code": 96,  # -> "60", in a slot marked unused (255): skipped
        "iu3_alarm_code": "96",
        "iu3_system_no": 1,
        "iu3_addr_no": "12",
    }
    assert alarms(status) == [
        {
            "code": "0B",
            "system_no": "00",
            "indoor_unit_no": "03",
            "description": TROUBLE_CODES["0B"][0],
        },
        {
            "code": "60",
            "system_no": "01",
            "indoor_unit_no": "12",
            **describe_code("60"),
        },
    ]
    assert alarms(FIXTURE["status"]) == []
    # Like the app (Number(undefined) !== 255): a slot without system /
    # address number is still listed, without those fields.
    assert alarms({"iu1_alarm_code": "96"}) == [{"code": "60", **describe_code("60")}]
    assert alarms({"iu1_alarm_code": "96", "iu1_system_no": 2}) == [
        {"code": "60", "system_no": "02", **describe_code("60")}
    ]
    assert alarms({"iu1_alarm_code": "96", "iu1_addr_no": 255}) == []
    assert alarms({}) is None
    assert alarms({**FIXTURE["status"], "iu5_alarm_code": "x"}) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", False),
        (0, False),
        ("", False),
        (None, False),
        (False, False),
        ("3", True),
        (3, True),
        ("00", True),  # truthy and not "0" in JavaScript
        (True, True),
    ],
)
def test_raw_alarm_follows_app_icon(value, expected) -> None:
    """getIsAlarm: some(e => e && "0" !== e), unused (255) slots included."""
    status = {**FIXTURE["status"], "iu2_alarm_code": value}
    assert FIXTURE["status"]["iu2_system_no"] == 255
    assert raw_alarm(status) is expected
    # The alarm list skips the unused slot either way.
    if value not in (None, "", False, True):
        assert alarms(status) == []


# ------------------------------------------------------------ running state


def test_running_state_fixture_idle() -> None:
    assert running_state({**FIXTURE["device"]["constantJson"], **FIXTURE["status"]}) == "idle"
    assert running_state({}) == "idle"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"self_clean": 1}, "self_clean"),
        ({"self_clean": "1"}, "self_clean"),  # loose == in the app
        ({"hig_temperature_sterilization": 1}, "sterilizing"),
        ({"hig_temperature_sterilization": "1"}, "sterilizing"),
        ({"self_clean": 2}, "cooling_down"),
        ({"self_clean": "2"}, "cooling_down"),
        ({"otaFlg": "1"}, "ota"),
        ({"otaFlg": 1}, "ota"),
        ({"comfortable": "1"}, "comfortable"),
        ({"comfortable": 1}, "comfortable"),
        ({"energy_saving": "1"}, "energy_saving"),
        ({"self_clean": 3}, "idle"),
        ({"self_clean": "x", "comfortable": "0", "energy_saving": "0"}, "idle"),
        # getXkqMaskingFlg's priority order.
        ({"self_clean": 1, "hig_temperature_sterilization": 1}, "self_clean"),
        ({"self_clean": 2, "hig_temperature_sterilization": 1}, "sterilizing"),
        ({"self_clean": 2, "otaFlg": "1"}, "cooling_down"),
        ({"otaFlg": "1", "comfortable": "1"}, "ota"),
        ({"comfortable": "1", "energy_saving": "1"}, "comfortable"),
    ],
)
def test_running_state_priority(overrides, expected) -> None:
    assert running_state({**FIXTURE["status"], **overrides}) == expected
    assert expected in RUNNING_STATES


def test_cooling_down() -> None:
    # The app checks sterilization before self_clean == 2 (its own dialog).
    assert not cooling_down({"self_clean": 2, "hig_temperature_sterilization": 1})
    assert cooling_down({"self_clean": 2})
    assert cooling_down({"self_clean": "2"})
    assert not cooling_down({"self_clean": 1})
    assert not cooling_down({"self_clean": "x"})
    assert not cooling_down(FIXTURE["status"])


# ------------------------------------------------------------------ banners


def test_banner_values() -> None:
    assert banner_on({"defrost_display": "1"}, "defrost_display") is True
    assert banner_on({"defrost_display": 1}, "defrost_display") is True
    assert banner_on({"defrost_display": "0"}, "defrost_display") is False
    assert banner_on({"defrost_display": "2"}, "defrost_display") is False
    assert banner_on({"defrost_display": "x"}, "defrost_display") is None
    assert banner_on({}, "defrost_display") is None
    assert has_banner({"defrost_display": "0"}, "defrost_display")
    assert not has_banner({}, "defrost_display")


def test_communication_exception_needs_3d_sensor() -> None:
    """allowShowTX = "1" === communication_exception && "2" === human_sense_type2."""
    key = "communication_exception"
    assert not has_banner({key: "1"}, key)
    assert not has_banner({key: "1", "human_sense_type2": "0"}, key)
    assert has_banner({key: "1", "human_sense_type2": "2"}, key)
    assert banner_on({key: "1", "human_sense_type2": "2"}, key) is True
    assert banner_on({key: "1", "human_sense_type2": "0"}, key) is False
    # The 3D sensor check comes first for every value type.
    assert banner_on({key: True, "human_sense_type2": "0"}, key) is False
    assert banner_on({key: "x", "human_sense_type2": "0"}, key) is False
    assert banner_on({key: True, "human_sense_type2": "2"}, key) is True
    assert banner_on({"human_sense_type2": "2"}, key) is None


async def test_banners_from_fixture(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    ent_reg = er.async_get(hass)
    for key, enabled in STATUS_BANNERS.items():
        entity_id = _entity(hass, "binary_sensor", key)
        if key == "communication_exception":
            # Not reported, and the author's unit has no 3D human sensor.
            assert entity_id is None
            continue
        assert entity_id, key
        entry = ent_reg.async_get(entity_id)
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert enabled is (key in BANNERS_ON_BY_DEFAULT)
        if enabled:
            assert entry.disabled_by is None
            assert hass.states.get(entity_id).state == "off"
        else:
            assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert hass.states.get(entity_id) is None
    conflict = _state(hass, "binary_sensor", "mode_confict")
    assert conflict.attributes["device_class"] == BinarySensorDeviceClass.PROBLEM
    assert "device_class" not in _state(hass, "binary_sensor", "defrost_display").attributes
    # Translation keys use the correct spelling; the unique IDs keep the app's.
    assert conflict.entity_id.endswith("_mode_conflict")


async def test_banner_states(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(
        mock_api,
        {
            **FIXTURE["status"],
            "defrost_display": "1",
            "pre_heat_display": "bad",
            "mode_confict": 1,
            "water_pump_status": "1",
        },
    )
    await _setup(hass, config_entry, enable=("water_pump_status",))
    assert _state(hass, "binary_sensor", "defrost_display").state == "on"
    assert _state(hass, "binary_sensor", "pre_heat_display").state == "unknown"
    assert _state(hass, "binary_sensor", "mode_confict").state == "on"
    assert _state(hass, "binary_sensor", "central_controller").state == "off"
    assert _state(hass, "binary_sensor", "water_pump_status").state == "on"


async def test_banners_need_their_keys(hass: HomeAssistant, mock_api, config_entry) -> None:
    status = {k: v for k, v in FIXTURE["status"].items() if k not in STATUS_BANNERS}
    status["central_controller"] = "1"
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    for key in STATUS_BANNERS:
        if key != "central_controller":
            assert _entity(hass, "binary_sensor", key) is None, key
    assert _state(hass, "binary_sensor", "central_controller").state == "on"


async def test_communication_exception_with_3d_sensor(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A synthetic unit with the 3D human sensor gets the 人感通讯异常 banner."""
    _constants(mock_api, human_sense_type2="2")
    _with_status(mock_api, {**FIXTURE["status"], "communication_exception": "1"})
    await _setup(hass, config_entry, enable=("communication_exception",))
    entry = er.async_get(hass).async_get(_entity(hass, "binary_sensor", "communication_exception"))
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert _state(hass, "binary_sensor", "communication_exception").state == "on"


async def test_communication_exception_without_3d_sensor(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _with_status(mock_api, {**FIXTURE["status"], "communication_exception": "1"})
    await _setup(hass, config_entry)
    assert _entity(hass, "binary_sensor", "communication_exception") is None


async def test_communication_exception_default_disabled(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _constants(mock_api, human_sense_type2="2")
    _with_status(mock_api, {**FIXTURE["status"], "communication_exception": "0"})
    await _setup(hass, config_entry)
    entry = er.async_get(hass).async_get(_entity(hass, "binary_sensor", "communication_exception"))
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


# ------------------------------------------------------ entities / commands


async def test_running_state_sensor(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    state = _state(hass, "sensor", "running_state")
    assert state.state == "idle"
    assert state.attributes["device_class"] == SensorDeviceClass.ENUM
    assert state.attributes["options"] == list(RUNNING_STATES)

    _with_status(mock_api, {**FIXTURE["status"], "hig_temperature_sterilization": 1})
    await config_entry.runtime_data.async_refresh()
    assert _state(hass, "sensor", "running_state").state == "sterilizing"


async def test_fault_code_sensor(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    state = _state(hass, "sensor", "fault_code")
    assert state.state == "none"
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(state.entity_id).entity_category is EntityCategory.DIAGNOSTIC
    fault = _state(hass, "binary_sensor", "fault")
    assert fault.attributes == {**fault.attributes, "alarms": [], "raw_alarm": False}

    _with_status(
        mock_api,
        {
            **FIXTURE["status"],
            "iu1_alarm_code": "96",
            "iu2_alarm_code": "3",  # unused slot: only the app's red icon
        },
    )
    await config_entry.runtime_data.async_refresh()
    state = _state(hass, "sensor", "fault_code")
    assert state.state == "60"
    assert state.attributes["system_no"] == "00"
    assert state.attributes["indoor_unit_no"] == "03"
    assert state.attributes["description"] == describe_code("60")["description"]
    assert state.attributes["reason_two"] == describe_code("60")["reason_two"]
    fault = _state(hass, "binary_sensor", "fault")
    assert fault.state == "on"
    assert [a["code"] for a in fault.attributes["alarms"]] == ["60"]
    assert fault.attributes["raw_alarm"] is True


async def test_raw_alarm_without_listed_alarm(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, {**FIXTURE["status"], "iu2_alarm_code": "3"})
    await _setup(hass, config_entry)
    fault = _state(hass, "binary_sensor", "fault")
    assert fault.state == "off"
    assert fault.attributes["raw_alarm"] is True
    assert _state(hass, "sensor", "fault_code").state == "none"


async def test_fault_code_unknown_without_alarm_fields(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    status = {k: v for k, v in FIXTURE["status"].items() if "alarm_code" not in k}
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    state = _state(hass, "sensor", "fault_code")
    assert state.state == "unknown"
    assert "description" not in state.attributes


def _actions(hass):
    return [
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE),
            {ATTR_HVAC_MODE: HVACMode.HEAT},
        ),
        (BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"), {}),
        (SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_lock"), {}),
        (SWITCH_DOMAIN, SERVICE_TURN_OFF, _entity(hass, SWITCH_DOMAIN, "auto_brightness"), {}),
    ]


async def test_commands_refused_while_cooling_down(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _with_status(mock_api, {**FIXTURE["status"], "self_clean": 2})
    await _setup(hass, config_entry)
    assert _state(hass, "sensor", "running_state").state == "cooling_down"
    for domain, service, entity_id, data in _actions(hass):
        with pytest.raises(HomeAssistantError) as info:
            await hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
            )
        assert info.value.translation_key == "cooling_down"
    mock_api.control.assert_not_called()


async def test_cooling_down_found_by_fresh_read(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The settings' re-read before sending sees the cooling down and stops."""
    await _setup(hass, config_entry)
    ts = FIXTURE["status"]["timestamp"]
    _with_status(
        mock_api,
        {**FIXTURE["status"], "timestamp": ts + 1000, "roundTime": ts + 1000, "self_clean": "2"},
    )
    with pytest.raises(HomeAssistantError) as info:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: _entity(hass, SWITCH_DOMAIN, "key_lock")},
            blocking=True,
        )
    assert info.value.translation_key == "cooling_down"
    mock_api.control.assert_not_called()


async def _yield(entry, mock_api) -> None:
    """The app logs in: the coordinator leaves the session to it."""
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.session_yielded
    mock_api.get_status.side_effect = good


async def test_cooling_down_found_after_reclaim(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Taking the session back re-reads the state and checks it again."""
    await _setup(hass, config_entry)
    ts = FIXTURE["status"]["timestamp"]
    cooling = {**FIXTURE["status"], "timestamp": ts + 1000, "roundTime": ts + 1000, "self_clean": 2}
    for domain, service, entity_id, data in _actions(hass)[:3:2]:  # climate, switch
        await _yield(config_entry, mock_api)
        _with_status(mock_api, cooling)
        with pytest.raises(HomeAssistantError) as info:
            await hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
            )
        assert info.value.translation_key == "cooling_down"
        assert not config_entry.runtime_data.session_yielded
    mock_api.control.assert_not_called()


async def test_stale_cooling_down_does_not_block_reclaim(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A cooling down cached before the yield is re-read, not refused outright."""
    _with_status(mock_api, {**FIXTURE["status"], "self_clean": 2})
    await _setup(hass, config_entry)
    await _yield(config_entry, mock_api)
    ts = FIXTURE["status"]["timestamp"]
    _with_status(
        mock_api,
        {**FIXTURE["status"], "timestamp": ts + 1000, "roundTime": ts + 1000, "self_clean": 0},
    )
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: _entity(hass, SWITCH_DOMAIN, "key_lock")},
        blocking=True,
    )
    mock_api.control.assert_called_once()
    assert not config_entry.runtime_data.session_yielded


async def test_self_clean_running_not_refused(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Only the cooling down (self_clean == 2) refuses control."""
    _with_status(mock_api, {**FIXTURE["status"], "self_clean": 1})
    await _setup(hass, config_entry)
    assert _state(hass, "sensor", "running_state").state == "self_clean"
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: _entity(hass, SWITCH_DOMAIN, "key_lock")},
        blocking=True,
    )
    mock_api.control.assert_called_once()


async def test_status_keys_in_diagnostics(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, {**FIXTURE["status"], "comfortable": "0", "energy_saving": "0"})
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    device = diag["devices"][0]
    status = device["status"]
    for key in (
        *(k for k in STATUS_BANNERS if k != "communication_exception"),
        "self_clean",
        "hig_temperature_sterilization",
        "comfortable",
        "energy_saving",
        "iu2_system_no",
        "iu2_addr_no",
    ):
        assert key in status, key
    assert status["iu2_system_no"] == 255
    assert device["constants"]["human_sense_type2"] == "0"
