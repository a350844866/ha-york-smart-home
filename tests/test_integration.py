"""Home Assistant level tests for York Smart Home."""

from __future__ import annotations

import copy
from datetime import timedelta
import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.york_smart_home.api import (
    Session,
    YorkAuthError,
    YorkCommandError,
    YorkConnectionError,
    YorkLoginError,
    YorkRateLimitError,
    YorkSessionConflictError,
)
from custom_components.york_smart_home.const import DOMAIN
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.trouble_codes import APP_ALARM_TEXTS
from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import CODE, FIXTURE, PHONE


async def _setup(hass: HomeAssistant, entry) -> str:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)


async def _call(hass, service, entity_id, **data):
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


def _last_ctrl(mock_api) -> dict:
    return mock_api.control.call_args.args[4]


# ---------------------------------------------------------------- config flow


async def test_password_flow(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "password"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "123", CONF_PASSWORD: "x"}
    )
    assert result["errors"] == {"phone": "invalid_phone"}

    mock_api.login_password.side_effect = YorkAuthError("bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE, CONF_PASSWORD: "x"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api.login_password.side_effect = YorkConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE, CONF_PASSWORD: "x"}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_api.login_password.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": f" {PHONE} ", CONF_PASSWORD: "pw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "200002"
    data = result["data"]
    assert data["phone"] == PHONE and data[CONF_PASSWORD] == "pw"
    assert data["session"]["token"] == "tok"
    assert len(data["client_id"]) == 19  # generated, never empty


async def test_sms_flow(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "sms"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"phone": PHONE})
    assert result["step_id"] == "sms_code"
    mock_api.request_sms_code.assert_awaited_once()

    mock_api.login_sms.side_effect = YorkAuthError("bad code")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verify_code": "000000"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api.login_sms.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verify_code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] is None
    mock_api.login_sms.assert_awaited_with("123456")


async def test_duplicate_account(hass: HomeAssistant, mock_api, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "password"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE, CONF_PASSWORD: "pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "password"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE, CONF_PASSWORD: "newpw"}
    )
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "newpw"
    assert config_entry.data["client_id"] == "cid"  # push id kept

    # Logging into a different account is refused.
    mock_api.auth.user_id = 1
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "password"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE, CONF_PASSWORD: "x"}
    )
    assert result["reason"] == "wrong_account"


async def test_options(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 60}
    )
    await hass.async_block_till_done()
    assert config_entry.runtime_data.update_interval.total_seconds() == 60


# ---------------------------------------------------------------------- state


async def test_state(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    state = hass.states.get(entity_id)
    assert state.state == HVACMode.COOL
    attrs = state.attributes
    assert attrs["current_temperature"] == 27
    assert attrs["temperature"] == 27.5
    assert attrs["target_temp_step"] == 0.5
    assert attrs["min_temp"] == 19 and attrs["max_temp"] == 30
    assert attrs["fan_mode"] == "low"
    assert attrs["fan_modes"] == ["low", "medium", "high"]
    assert attrs["hvac_modes"] == ["off", "cool", "heat", "fan_only", "dry"]

    ent_reg = er.async_get(hass)
    temp_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{CODE}_indoor_temperature")
    assert hass.states.get(temp_id).state == "27"
    fault_id = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{CODE}_fault")
    assert hass.states.get(fault_id).state == "off"
    filter_id = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{CODE}_filter")
    assert hass.states.get(filter_id).state == "off"


async def test_fault_and_negative_temperature(hass: HomeAssistant, mock_api, config_entry) -> None:
    status = {
        **FIXTURE["status"],
        "iu1_alarm_code": "3",
        "iu1_system_no": 0,
        "iu1_addr_no": 3,
        "indoor_temperature": 251,
    }
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    await _setup(hass, config_entry)
    ent_reg = er.async_get(hass)
    fault = hass.states.get(ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{CODE}_fault"))
    assert fault.state == "on"
    assert fault.attributes["alarms"] == [
        {
            "code": "03",
            "system_no": "00",
            "indoor_unit_no": "03",
            # The app's own text for 03 (getAlarmInfo), not the table's.
            "description": APP_ALARM_TEXTS["03"],
        }
    ]
    # The first alarm's code and description, also on their own.
    assert fault.attributes["code"] == "03"
    assert fault.attributes["description"] == APP_ALARM_TEXTS["03"]
    assert fault.attributes["raw_alarm"] is True
    temp_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{CODE}_indoor_temperature")
    assert hass.states.get(temp_id).state == "-5"


async def test_offline_unavailable(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_online.return_value = {}  # missing counts as offline
    coordinator._last_online = None
    await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "unavailable"
    with pytest.raises(HomeAssistantError):
        await coordinator.async_request_change(CODE, power=True)
    mock_api.control.assert_not_called()


async def test_online_polled_less_often(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    status_calls = mock_api.get_status.call_count
    await config_entry.runtime_data.async_refresh()
    assert mock_api.get_status.call_count == status_calls + 1
    assert mock_api.get_online.call_count == 1


async def test_new_device_discovered_later(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    second = copy.deepcopy(FIXTURE["device"])
    second["xkqCode"] = "0000000000000002"
    mock_api.get_wire_controllers.return_value = [copy.deepcopy(FIXTURE["device"]), second]
    mock_api.get_status.side_effect = lambda home, codes: {
        c: copy.deepcopy(FIXTURE["status"]) for c, _ in codes
    }
    mock_api.get_online.return_value = {CODE: True, second["xkqCode"]: True}
    coordinator = config_entry.runtime_data
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    ent_id = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, "0000000000000002")
    assert hass.states.get(ent_id).state == HVACMode.COOL


# ------------------------------------------------------------------- commands


async def test_commands(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)

    await _call(hass, SERVICE_SET_TEMPERATURE, entity_id, **{ATTR_TEMPERATURE: 26})
    assert _last_ctrl(mock_api) == {"set_temp": 26, "set_temperature_add05": "0"}
    await _call(hass, SERVICE_SET_TEMPERATURE, entity_id, **{ATTR_TEMPERATURE: 24.5})
    assert _last_ctrl(mock_api) == {"set_temp": 24, "set_temperature_add05": "1"}

    await _call(hass, SERVICE_SET_FAN_MODE, entity_id, **{ATTR_FAN_MODE: "high"})
    assert _last_ctrl(mock_api) == {"wire_controller_wind_high": "1"}

    # Only what differs is sent (the unit is already on).
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert _last_ctrl(mock_api) == {"wire_controller_heating": "1"}
    assert mock_api.control.call_args.args[:4] == (100001, CODE, "C", "9")

    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.OFF})
    assert _last_ctrl(mock_api) == {"if_run": "0"}


async def test_mode_and_temperature_together(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        entity_id,
        **{ATTR_TEMPERATURE: 22.5, ATTR_HVAC_MODE: HVACMode.HEAT},
    )
    assert _last_ctrl(mock_api) == {
        "wire_controller_heating": "1",
        "set_temp": 22,
        "set_temperature_add05": "1",
    }


async def test_power_on_from_off(hass: HomeAssistant, mock_api, config_entry) -> None:
    off_status = {**FIXTURE["status"], "if_run": "0"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(off_status)}
    entity_id = await _setup(hass, config_entry)
    assert hass.states.get(entity_id).state == HVACMode.OFF
    coordinator = config_entry.runtime_data

    # Power on and switch mode in one command.
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.DRY})
    assert _last_ctrl(mock_api) == {"if_run": "1", "wire_controller_rmwet": "1"}

    # The controller ignored it and keeps reporting off: after the grace
    # period its report wins.
    ts = FIXTURE["status"]["timestamp"]
    mock_api.get_status.side_effect = lambda home, codes: {
        CODE: {**off_status, "timestamp": ts + 30000}
    }
    with patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + 60,
    ):
        await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.OFF

    # Like the app, only the changed field is sent: cool is already selected.
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert _last_ctrl(mock_api) == {"if_run": "1"}


async def test_optimistic_update(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.HEAT})
        await _call(hass, SERVICE_SET_FAN_MODE, entity_id, **{ATTR_FAN_MODE: "medium"})
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.HEAT
    assert state.attributes["fan_mode"] == "medium"
    assert state.attributes["min_temp"] == 16 and state.attributes["max_temp"] == 32


async def test_command_rejected(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    mock_api.control.side_effect = YorkCommandError("空调不在线")
    with pytest.raises(HomeAssistantError) as info:
        await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert info.value.translation_key == "command_rejected"
    assert info.value.translation_placeholders == {"message": "空调不在线"}
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == HVACMode.COOL  # no optimistic change


async def test_command_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    entity_id = await _setup(hass, config_entry)
    mock_api.control.side_effect = YorkAuthError("expired")
    with pytest.raises(HomeAssistantError) as info:
        await _call(hass, "turn_off", entity_id)
    assert info.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


# ------------------------------------------------------------------ lifecycle


async def test_session_persisted_without_reload(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    assert mock_api.ctor_kwargs["auth"] == Session("tok", 200002, "rt", 0)
    on_session = mock_api.ctor_kwargs["on_session"]
    on_session(Session("new-token", 200002, "rt2", 1))
    await hass.async_block_till_done()
    assert config_entry.data["session"]["token"] == "new-token"
    assert config_entry.data["session"]["refresh_token"] == "rt2"
    assert config_entry.state is ConfigEntryState.LOADED
    assert mock_api.get_wire_controllers.call_count == 1  # no reload


async def test_auth_failure_at_setup_starts_reauth(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    mock_api.get_wire_controllers.side_effect = YorkAuthError("expired")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_unload(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_redacted(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = repr(diag)
    for secret in (PHONE, "tok", "pw", "cid", CODE, FIXTURE["device"]["macImei"], "100001"):
        assert secret not in text, secret
    device = diag["devices"][0]
    assert device["status"]["if_run"] == "1"
    assert device["status"]["roundTime"] == FIXTURE["status"]["roundTime"]
    assert device["status"]["otaFlg"] == "0"
    assert device["constants"]["wire_controller_makecold_fix"] == "1"
    assert device["constants"]["run_model_fixed"] == "0"
    assert device["constants"]["cold_down_value"] == 19
    assert device["constants"]["controllerFloorHeatFix"] == "0"
    assert not any(k.startswith("member") for k in device["constants"])
    assert device["info"]["versionMcu"] == FIXTURE["device"]["versionMcu"]
    assert diag["login_method"] == "password"
    assert diag["session_yielded"] is False and diag["client_outdated"] is False


# ----------------------------------------------------------------- robustness


def _entity(hass, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{CODE}_{key}")


def _as_ints(values: dict) -> dict:
    return {k: (int(v) if v in ("0", "1") else v) for k, v in values.items()}


async def test_integer_flags(hass: HomeAssistant, mock_api, config_entry) -> None:
    status = _as_ints(FIXTURE["status"])
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    device = copy.deepcopy(FIXTURE["device"])
    device["constantJson"] = _as_ints(device["constantJson"])
    mock_api.get_wire_controllers.return_value = [device]
    entity_id = await _setup(hass, config_entry)
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.COOL
    assert state.attributes["fan_modes"] == ["low", "medium", "high"]
    assert state.attributes["temperature"] == 27.5
    assert state.attributes["target_temp_step"] == 0.5


async def test_missing_or_bad_data_is_unknown(hass: HomeAssistant, mock_api, config_entry) -> None:
    status = {
        k: v
        for k, v in FIXTURE["status"].items()
        if "alarm_code" not in k and not k.startswith("filter_life_tips")
    }
    status["indoor_temperature"] = "bad"
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(status)}
    await _setup(hass, config_entry)
    assert hass.states.get(_entity(hass, "binary_sensor", "fault")).state == "unknown"
    assert hass.states.get(_entity(hass, "binary_sensor", "filter")).state == "unknown"
    assert hass.states.get(_entity(hass, "sensor", "indoor_temperature")).state == "unknown"


def _constants(**overrides):
    return lambda home, code: {**FIXTURE["device"]["constantJson"], **overrides}


async def test_locks(hass: HomeAssistant, mock_api, config_entry) -> None:
    # The app reads the locks from the controller detail's constants.
    mock_api.get_wire_controller_detail.side_effect = _constants(
        run_model_fixed="1", set_temp_fixed="1", B9="1"
    )
    entity_id = await _setup(hass, config_entry)
    attrs = hass.states.get(entity_id).attributes
    assert attrs["mode_locked"] and attrs["temperature_locked"] and attrs["fan_locked"]

    for service, data, key in (
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.HEAT}, "mode_locked"),
        (SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 25}, "temperature_locked"),
        (SERVICE_SET_FAN_MODE, {ATTR_FAN_MODE: "high"}, "fan_locked"),
    ):
        with pytest.raises(ServiceValidationError) as info:
            await _call(hass, service, entity_id, **data)
        assert info.value.translation_domain == DOMAIN
        assert info.value.translation_key == key
    mock_api.control.assert_not_called()

    # The current (locked) mode can still be chosen, and power still works.
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.COOL})
    mock_api.control.assert_not_called()  # already on in cool: nothing to send
    await _call(hass, "turn_off", entity_id)
    assert _last_ctrl(mock_api) == {"if_run": "0"}


async def test_stale_and_partial_status(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    ts = FIXTURE["status"]["timestamp"]
    stale = {**FIXTURE["status"], "timestamp": ts - 60000, "roundTime": ts - 60000, "if_run": "0"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(stale)}
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.COOL

    partial = {"xkqCode": CODE, "timestamp": ts + 1000, "wire_controller_heating": "1"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(partial)}
    await config_entry.runtime_data.async_refresh()
    state = hass.states.get(entity_id)
    assert state.state == HVACMode.HEAT  # cool flag cleared, other fields kept
    assert state.attributes["temperature"] == 27.5


async def test_rate_limit_pauses_polling(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkRateLimitError(300)
    await coordinator.async_refresh()
    calls = mock_api.get_status.call_count
    for _ in range(4):
        await coordinator.async_refresh()
    assert mock_api.get_status.call_count == calls  # paused, cloud not contacted
    assert not coordinator.last_update_success  # unavailable after 5 failed polls


async def test_session_conflict_starts_reauth(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    mock_api.get_status.side_effect = YorkSessionConflictError("replaced")
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_login_rejected_message(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "password"}
    )
    mock_api.login_password.side_effect = YorkLoginError("账号未注册")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE, CONF_PASSWORD: "x"}
    )
    assert result["errors"] == {"base": "login_rejected"}
    assert result["description_placeholders"]["message"] == "账号未注册"


# ------------------------------------------------------------------ migration


@pytest.mark.parametrize(
    ("old_data", "expected_session"),
    [
        (  # 0.1.x layout
            {
                "phone": PHONE,
                CONF_PASSWORD: "pw",
                "jg_reg_id": "phone-push-id",
                "token": "tok",
                "user_id": 200002,
            },
            {"token": "tok", "user_id": 200002, "refresh_token": None, "login_version": 0},
        ),
        (  # 0.2.0 layout, also stored as version 1
            {
                "phone": PHONE,
                CONF_PASSWORD: None,
                "client_id": "cid",
                "session": {
                    "token": "tok",
                    "user_id": 200002,
                    "refresh_token": "rt",
                    "login_version": 2,
                },
            },
            {"token": "tok", "user_id": 200002, "refresh_token": "rt", "login_version": 2},
        ),
    ],
)
async def test_migration(
    hass: HomeAssistant, mock_api, old_data: dict, expected_session: dict
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, unique_id="200002", data=old_data)
    entity_id = await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert entry.data["session"] == expected_session
    assert entry.data["client_id"] and entry.data["client_id"] != "phone-push-id"
    for key in ("token", "user_id", "jg_reg_id"):
        assert key not in entry.data
    assert entity_id is not None  # unique_id (xkqCode) unchanged since 0.1.x


async def test_future_version_not_loaded(hass: HomeAssistant, mock_api, config_entry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=dict(config_entry.data))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


# ------------------------------------------------------------- app behavior


async def test_no_command_when_already_in_state(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    entity_id = await _setup(hass, config_entry)
    await _call(hass, "turn_on", entity_id)
    await _call(hass, SERVICE_SET_TEMPERATURE, entity_id, **{ATTR_TEMPERATURE: 27.5})
    await _call(hass, SERVICE_SET_FAN_MODE, entity_id, **{ATTR_FAN_MODE: "low"})
    mock_api.control.assert_not_called()


async def test_incremental_polling(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert mock_api.get_status.call_args_list[0].args[1] == [(CODE, 0)]  # full state first

    ts = FIXTURE["status"]["timestamp"]
    mock_api.get_status.side_effect = lambda home, codes: {}  # no news
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, ts)]
    assert (
        hass.states.get(er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)).state
        == HVACMode.COOL
    )  # an empty answer keeps the state

    coordinator._last_discovery = None  # periodic rediscovery re-reads the full state
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]


async def test_new_report_after_command_wins(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await _call(hass, SERVICE_SET_TEMPERATURE, entity_id, **{ATTR_TEMPERATURE: 28})
    assert hass.states.get(entity_id).attributes["temperature"] == 28  # shown right away

    # No new report yet: keep showing the requested value.
    mock_api.get_status.side_effect = lambda home, codes: {}
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).attributes["temperature"] == 28

    # A report sent before the command applied does not flip it back...
    ts = FIXTURE["status"]["timestamp"]
    report = {**FIXTURE["status"], "timestamp": ts + 5000}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(report)}
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).attributes["temperature"] == 28

    # ...but once the grace period is over, the controller's report wins.
    report = {**FIXTURE["status"], "timestamp": ts + 30000}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(report)}
    with patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + 60,
    ):
        await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).attributes["temperature"] == 27.5


async def test_stale_report_after_mode_change(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Seen live: right after 'heat' the controller reported the old mode once."""
    entity_id = await _setup(hass, config_entry)
    ts = FIXTURE["status"]["timestamp"]
    await _call(hass, SERVICE_SET_HVAC_MODE, entity_id, **{ATTR_HVAC_MODE: HVACMode.HEAT})

    stale = {**FIXTURE["status"], "timestamp": ts + 1000}  # still cool
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(stale)}
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.HEAT

    applied = {
        **FIXTURE["status"],
        "timestamp": ts + 2000,
        "wire_controller_makecold": "0",
        "wire_controller_heating": "1",
    }
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(applied)}
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.HEAT
    assert not config_entry.runtime_data.devices[CODE].expected  # confirmed

    # Later changes from the panel are shown normally.
    panel = {
        **applied,
        "timestamp": ts + 3000,
        "wire_controller_heating": "0",
        "wire_controller_rmwet": "1",
    }
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(panel)}
    await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.DRY


async def test_commands_sent_immediately(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    await _call(hass, SERVICE_SET_TEMPERATURE, entity_id, **{ATTR_TEMPERATURE: 26})
    assert mock_api.control.call_count == 1
    await _call(hass, SERVICE_SET_FAN_MODE, entity_id, **{ATTR_FAN_MODE: "high"})
    assert mock_api.control.call_count == 2
    # The second command is diffed against the state after the first one.
    assert _last_ctrl(mock_api) == {"wire_controller_wind_high": "1"}


async def test_transient_failures_tolerated(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    good = mock_api.get_status.side_effect

    mock_api.get_status.side_effect = YorkConnectionError("ServerDisconnectedError")
    for _ in range(4):
        await coordinator.async_refresh()
        assert hass.states.get(entity_id).state == HVACMode.COOL  # last state kept

    # A success resets the count.
    mock_api.get_status.side_effect = good
    await coordinator.async_refresh()
    mock_api.get_status.side_effect = YorkConnectionError("ServerDisconnectedError")
    for _ in range(4):
        await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.COOL

    # The fifth failure in a row makes the entities unavailable.
    await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "unavailable"

    mock_api.get_status.side_effect = good
    await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_auth_failure_not_tolerated(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    mock_api.get_status.side_effect = YorkAuthError("expired")
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_polls_every_scan_interval(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Regression: the regular 2s polling must really happen (not ~10s)."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.util import dt as dt_util

    await _setup(hass, config_entry)
    assert config_entry.runtime_data.update_interval.total_seconds() == 2
    start = mock_api.get_status.call_count
    now = dt_util.utcnow()
    for i in range(1, 6):
        async_fire_time_changed(hass, now + timedelta(seconds=2.1 * i))
        await hass.async_block_till_done()
    assert mock_api.get_status.call_count - start == 5
