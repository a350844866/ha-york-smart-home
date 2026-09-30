"""Sharing the single-login account with the app, forced app updates and OTA."""

from __future__ import annotations

from contextlib import contextmanager
import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.york_smart_home.api import (
    YorkClientOutdatedError,
    YorkConnectionError,
    YorkLoginError,
    YorkSessionTakenError,
)
from custom_components.york_smart_home.const import DOMAIN
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from .conftest import CODE, FIXTURE
from .test_integration import _last_ctrl, _setup


@contextmanager
def _later(seconds: float):
    """Run the coordinator ``seconds`` in the future."""
    with patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + seconds,
    ):
        yield


def _issue(hass: HomeAssistant, entry, key: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


def _reauth_started(hass: HomeAssistant) -> bool:
    return any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


# ------------------------------------------------------------- session yield


async def test_session_taken_yields(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Default: leave the session to the app instead of logging it out."""
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    good = mock_api.get_status.side_effect

    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    issue = _issue(hass, config_entry, "session_taken")
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {"minutes": "30"}
    assert coordinator.session_yielded

    # The cloud is not contacted during the yield period...
    calls = mock_api.get_status.call_count
    for _ in range(5):
        await coordinator.async_refresh()
    assert mock_api.get_status.call_count == calls
    mock_api.relogin.assert_not_called()
    assert not _reauth_started(hass)
    # ...and the entities become unavailable like after any failed polls.
    assert hass.states.get(entity_id).state == "unavailable"
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["session_yielded"] is True

    with _later(29 * 60):
        await coordinator.async_refresh()
    mock_api.relogin.assert_not_called()

    # Afterwards HA logs in again and polls normally.
    mock_api.get_status.side_effect = good
    with _later(31 * 60):
        await coordinator.async_refresh()
    mock_api.relogin.assert_awaited_once()
    assert not coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is None
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_yield_minutes_option(hass: HomeAssistant, mock_api, config_entry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="200002",
        data=dict(config_entry.data),
        options={"scan_interval": 2, "reclaim_session": False, "session_yield_minutes": 5},
    )
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    assert _issue(hass, entry, "session_taken").translation_placeholders == {"minutes": "5"}
    mock_api.get_status.side_effect = good
    with _later(6 * 60):
        await coordinator.async_refresh()
    mock_api.relogin.assert_awaited_once()


async def test_reclaim_option(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    assert mock_api.ctor_kwargs["reclaim_session"] is False
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    schema_keys = [str(key) for key in result["data_schema"].schema]
    assert schema_keys == ["scan_interval", "reclaim_session", "session_yield_minutes"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 2, "reclaim_session": True, "session_yield_minutes": 60},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert mock_api.reclaim_session is True
    assert config_entry.options["session_yield_minutes"] == 60


async def test_yield_minutes_range(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    for bad in (4, 721):
        with pytest.raises(vol.Invalid):
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"scan_interval": 2, "reclaim_session": False, "session_yield_minutes": bad},
            )


async def test_command_during_yield_reclaims(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A command is explicit intent: log in again now, re-read the state, then send."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    assert coordinator.session_yielded

    # Meanwhile the unit was switched to heat on the app.
    heating = {**FIXTURE["status"], "wire_controller_makecold": "0", "wire_controller_heating": "1"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(heating)}
    await coordinator.async_request_change(CODE, mode="heat", temperature=26.0)
    mock_api.relogin.assert_awaited_once()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]  # full state re-read
    assert _last_ctrl(mock_api) == {"set_temp": 26, "set_temperature_add05": "0"}
    assert not coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is None


async def test_command_during_yield_login_fails(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    mock_api.relogin.side_effect = YorkLoginError("密码错误")
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "auth_failed"
    mock_api.control.assert_not_called()
    await hass.async_block_till_done()
    assert _reauth_started(hass)


async def test_login_rejected_after_yield_starts_reauth(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    mock_api.relogin.side_effect = YorkLoginError("密码错误")
    with _later(31 * 60):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _reauth_started(hass)


async def test_yield_at_setup(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A restart while the app holds the session must not log the app out."""
    mock_api.get_wire_controllers.side_effect = YorkSessionTakenError("HTTP 401")
    entity_id = await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert entity_id is None  # no devices until the session is back
    assert _issue(hass, config_entry, "session_taken") is not None
    mock_api.relogin.assert_not_called()

    mock_api.get_wire_controllers.side_effect = None
    with _later(31 * 60):
        await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    mock_api.relogin.assert_awaited_once()
    entity_id = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_repair_flow_reclaims(hass: HomeAssistant, mock_api, config_entry) -> None:
    assert await async_setup_component(hass, "repairs", {})
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    issue = _issue(hass, config_entry, "session_taken")

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": issue.issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    # A custom fix flow must pass the issue's placeholders on itself.
    assert result["description_placeholders"] == {"minutes": "30"}

    mock_api.relogin.side_effect = YorkLoginError("密码错误")
    result = await manager.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "reclaim_failed"}
    assert result["description_placeholders"] == {"minutes": "30"}

    mock_api.relogin.side_effect = None
    mock_api.get_status.side_effect = good
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _issue(hass, config_entry, "session_taken") is None
    assert not coordinator.session_yielded
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_unload_removes_issues(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await config_entry.runtime_data.async_refresh()
    assert _issue(hass, config_entry, "session_taken") is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert _issue(hass, config_entry, "session_taken") is None


# ---------------------------------------------------------- forced app update


async def test_client_outdated_pauses_polling(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkClientOutdatedError("1.9.0", "https://example.invalid/a")
    await coordinator.async_refresh()
    issue = _issue(hass, config_entry, "client_outdated")
    assert issue is not None
    assert issue.translation_placeholders == {
        "version": "1.9.0",
        "url": "https://example.invalid/a",
    }
    assert coordinator.client_outdated

    calls = mock_api.get_status.call_count
    for _ in range(5):
        await coordinator.async_refresh()
    assert mock_api.get_status.call_count == calls  # paused
    assert hass.states.get(entity_id).state == "unavailable"

    mock_api.get_status.side_effect = good
    with _later(3601):
        await coordinator.async_refresh()
    assert _issue(hass, config_entry, "client_outdated") is None
    assert not coordinator.client_outdated
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_client_outdated_without_details(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    mock_api.get_status.side_effect = YorkClientOutdatedError(None, None)
    await config_entry.runtime_data.async_refresh()
    placeholders = _issue(hass, config_entry, "client_outdated").translation_placeholders
    assert placeholders == {"version": "?", "url": "-"}


async def test_client_outdated_at_login(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "password"}
    )
    mock_api.login_password.side_effect = YorkClientOutdatedError("1.9.0", None)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "13800000000", "password": "x"}
    )
    assert result["errors"] == {"base": "client_outdated"}


# ------------------------------------------------------------------------ OTA


async def test_ota_full_query_and_no_control(hass: HomeAssistant, mock_api, config_entry) -> None:
    ota = {**FIXTURE["status"], "otaFlg": "1"}
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(ota)}
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    # Home page: while otaFlg is on, every poll asks for the full state.
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]

    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "ota_in_progress"
    mock_api.control.assert_not_called()

    # Loose comparison like the app (1 == "1").
    ota["otaFlg"] = 1
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_request_change(CODE, power=False)

    # Update done: incremental polling resumes.
    ota["otaFlg"] = "0"
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, FIXTURE["status"]["roundTime"])]
    await coordinator.async_request_change(CODE, power=False)
    assert _last_ctrl(mock_api) == {"if_run": "0"}


# ------------------------------------------------------- yield edge cases


async def _yield(hass: HomeAssistant, mock_api, entry) -> None:
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.session_yielded
    mock_api.get_status.side_effect = good


async def _set_options(hass: HomeAssistant, entry, **options) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 2, "reclaim_session": False, "session_yield_minutes": 30, **options},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_failed_relogin_backs_off(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A cloud outage when the yield ends must not cause a login every poll."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    mock_api.relogin.side_effect = YorkConnectionError("HTTP 502")
    with _later(31 * 60):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 1
    with _later(31 * 60 + 2):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 1  # backing off
    assert coordinator.session_yielded
    assert not _reauth_started(hass)

    with _later(31 * 60 + 61):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 2
    # The next wait is longer.
    with _later(31 * 60 + 61 + 90):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 2

    mock_api.relogin.side_effect = None
    with _later(31 * 60 + 61 + 121):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 3
    assert not coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is None


async def test_reclaim_option_ends_yield(hass: HomeAssistant, mock_api, config_entry) -> None:
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    await _set_options(hass, config_entry, reclaim_session=True)
    await coordinator.async_refresh()
    mock_api.relogin.assert_awaited_once()
    assert not coordinator.session_yielded
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_shorter_yield_option(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    await _set_options(hass, config_entry, session_yield_minutes=10)
    assert _issue(hass, config_entry, "session_taken").translation_placeholders == {"minutes": "10"}
    with _later(9 * 60):
        await coordinator.async_refresh()
    mock_api.relogin.assert_not_called()
    with _later(11 * 60):
        await coordinator.async_refresh()
    mock_api.relogin.assert_awaited_once()
    assert not coordinator.session_yielded


async def test_command_taken_again_yields_without_reauth(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A second 401 right after the forced login of a command is not a bad password."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.control.side_effect = YorkSessionTakenError("HTTP 401")
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "session_taken"
    await hass.async_block_till_done()
    assert not _reauth_started(hass)
    assert coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is not None


async def test_command_taken_again_with_reclaim_option(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.reclaim_session = True
    mock_api.control.side_effect = YorkSessionTakenError("HTTP 401")
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "session_taken"
    await hass.async_block_till_done()
    assert not _reauth_started(hass)
    assert not coordinator.session_yielded


async def test_command_during_yield_reread_fails(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Never diff a command against state that could not be re-read."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    mock_api.get_status.side_effect = YorkConnectionError("timeout")
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "cannot_connect"
    assert "timeout" in info.value.translation_placeholders["error"]
    mock_api.control.assert_not_called()


async def test_detail_session_taken_yields(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A 401 on the controller detail is not swallowed by the constants fallback."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_wire_controller_detail.side_effect = YorkSessionTakenError("HTTP 401")
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    assert coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is not None
    mock_api.relogin.assert_not_called()


async def test_client_outdated_when_yield_ends(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    mock_api.relogin.side_effect = YorkClientOutdatedError("1.9.0", None)
    with _later(31 * 60):
        await coordinator.async_refresh()
    assert coordinator.client_outdated
    assert coordinator.session_yielded  # still to be ended
    assert _issue(hass, config_entry, "client_outdated") is not None
    with _later(31 * 60 + 600):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 1  # paused for an hour

    mock_api.relogin.side_effect = None
    with _later(31 * 60 + 3601):
        await coordinator.async_refresh()
    assert mock_api.relogin.await_count == 2
    assert not coordinator.session_yielded
    assert not coordinator.client_outdated
    assert _issue(hass, config_entry, "client_outdated") is None
    assert _issue(hass, config_entry, "session_taken") is None


# ------------------------------------------------- forced app update, more


async def test_client_outdated_at_setup(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Code 300 at startup loads the entry and keeps the hourly pause."""
    mock_api.get_wire_controllers.side_effect = YorkClientOutdatedError("1.9.0", None)
    entity_id = await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert entity_id is None
    assert _issue(hass, config_entry, "client_outdated") is not None
    coordinator = config_entry.runtime_data
    for _ in range(3):
        await coordinator.async_refresh()
    assert mock_api.get_wire_controllers.await_count == 1

    mock_api.get_wire_controllers.side_effect = None
    with _later(3601):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, config_entry, "client_outdated") is None
    entity_id = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)
    assert hass.states.get(entity_id).state == HVACMode.COOL


async def test_stale_client_outdated_issue_removed(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """An issue left by an earlier setup goes away after a successful poll."""
    config_entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"client_outdated_{config_entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="client_outdated",
        translation_placeholders={"version": "?", "url": "-"},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, config_entry, "client_outdated") is None


async def test_command_client_outdated(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.control.side_effect = YorkClientOutdatedError("1.9.0", None)
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "client_outdated"
    # The same pause as when polling hits it.
    assert coordinator.client_outdated
    assert _issue(hass, config_entry, "client_outdated") is not None
    calls = mock_api.get_status.call_count
    await coordinator.async_refresh()
    assert mock_api.get_status.call_count == calls


async def test_reclaim_client_outdated(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    mock_api.relogin.side_effect = YorkClientOutdatedError(None, None)
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_request_change(CODE, power=False)
    assert info.value.translation_key == "client_outdated"
    assert coordinator.client_outdated
    mock_api.control.assert_not_called()
