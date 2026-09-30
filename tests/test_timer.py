"""Simple timer (简易定时): API requests, state helpers, select and end sensor."""

from __future__ import annotations

import copy
from datetime import datetime
import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.york_smart_home.api import (
    Session,
    YorkCommandError,
    YorkConnectionError,
    YorkSessionTakenError,
)
from custom_components.york_smart_home.const import (
    API_BASE,
    COMMAND_GRACE,
    DOMAIN,
    TIMER_HOURS,
)
from custom_components.york_smart_home.control import (
    TIMER_OPTIONS,
    has_simple_timer,
    new_timer_turns_on,
    timer_active,
    timer_cancel_state,
    timer_end_ms,
    timer_fired,
    timer_option,
    timer_set_state,
    timer_turns_on,
)
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.select import YorkSimpleTimer
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import CODE, FIXTURE
from .test_api import OK, UNAUTHORIZED, _api, _login_body, _sequence

TIMER_URL = f"{API_BASE}/api/appdevice/homes/100001/xkqs/{CODE}/simpleTimer"
HOUR_MS = 3600 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _status(**overrides) -> dict:
    return {**FIXTURE["status"], **overrides}


def _with_status(mock_api, status: dict) -> None:
    mock_api.get_status.side_effect = lambda home, codes: {CODE: copy.deepcopy(status)}


def _timer_status(hours_left: float = 3, *, turns_on: bool = False, **overrides) -> dict:
    """A running timer as the cloud would report it (ints, like the app's model)."""
    timer = {
        "timerSimpleFlg": 1,
        "timerSimpleOnoff": 1 if turns_on else 0,
        "timerSimpleTime": _now_ms() + int(hours_left * HOUR_MS),
    }
    return _status(**{**timer, **overrides})


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{CODE}_{key}")


def _select(hass):
    return hass.states.get(_entity(hass, SELECT_DOMAIN, "simple_timer"))


def _end(hass):
    return hass.states.get(_entity(hass, "sensor", "timer_end"))


async def _choose(hass, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: _entity(hass, SELECT_DOMAIN, "simple_timer"), ATTR_OPTION: option},
        blocking=True,
    )


# -------------------------------------------------------------------- helpers


def test_timer_options_match_the_picker() -> None:
    """关闭定时, 0.5 .. 10 h in half hours, then 11 .. 24 h hourly."""
    assert len(TIMER_HOURS) == 34
    assert list(TIMER_OPTIONS)[:3] == ["0_5h", "1h", "1_5h"]
    assert "9_5h" in TIMER_OPTIONS and "10h" in TIMER_OPTIONS
    assert "10_5h" not in TIMER_OPTIONS and "11h" in TIMER_OPTIONS
    assert list(TIMER_OPTIONS)[-1] == "24h"
    assert all(TIMER_OPTIONS[timer_option(h)] == h for h in TIMER_HOURS)


def test_fixture_has_no_timer() -> None:
    """The user's controller: timerSimpleFlg null, timerSimpleTime 0, 255."""
    status = FIXTURE["status"]
    assert has_simple_timer(status)
    assert not timer_active(status)
    assert timer_end_ms(status, _now_ms()) is None
    assert not timer_fired(status, _now_ms())
    assert not has_simple_timer({"if_run": "1"})


def test_timer_state_helpers() -> None:
    now = _now_ms()
    status = _status(timerSimpleFlg=1, timerSimpleOnoff=1, timerSimpleTime=now + HOUR_MS)
    assert timer_active(status) and timer_turns_on(status)
    assert timer_end_ms(status, now) == now + HOUR_MS
    # The IU pages compare "1"; numeric strings are accepted too.
    loose = _status(timerSimpleFlg="1", timerSimpleOnoff="0", timerSimpleTime=str(now))
    assert timer_active(loose) and not timer_turns_on(loose)
    assert timer_end_ms(loose, now) == now
    # Implausible ends: not a number, 0, seconds instead of ms, > 25 h away.
    for value in (None, "soon", 0, now // 1000, now + 26 * HOUR_MS, now - 26 * HOUR_MS):
        assert timer_end_ms({**status, "timerSimpleTime": value}, now) is None
    # A time without the flag is no timer.
    assert timer_end_ms({**status, "timerSimpleFlg": 0}, now) is None
    assert not timer_fired({**status, "timerSimpleFlg": 0, "timerSimpleTime": 0}, now)


def test_timer_end_in_the_past() -> None:
    """Just past: still shown (the cloud may lag); over 10 minutes: fired."""
    now = _now_ms()
    status = _status(timerSimpleFlg=1, timerSimpleOnoff=0)
    just = {**status, "timerSimpleTime": now - 5 * 60 * 1000}
    assert timer_end_ms(just, now) == now - 5 * 60 * 1000
    assert not timer_fired(just, now)
    gone = {**status, "timerSimpleTime": now - 11 * 60 * 1000}
    assert timer_end_ms(gone, now) is None
    assert timer_fired(gone, now)
    assert not timer_fired({**status, "timerSimpleTime": now + HOUR_MS}, now)
    assert not timer_fired({**status, "timerSimpleTime": None}, now)


def test_new_timer_toggles_power() -> None:
    assert not new_timer_turns_on(_status(if_run="1"))
    assert new_timer_turns_on(_status(if_run="0"))
    assert new_timer_turns_on({})  # the app's isOn defaults to off


def test_optimistic_states() -> None:
    assert timer_set_state(0.5, False, {"timerSimpleTime": 1790765454000, "systemTime": 1}) == {
        "timerSimpleFlg": 1,
        "timerSimpleOnoff": 0,
        "timerSimpleTime": 1790765454000,
    }
    # An answer without a usable time: the end is unknown, not the old timer's.
    assert timer_set_state(2, True, {"timerSimpleTime": None})["timerSimpleTime"] is None
    assert timer_set_state(2, True, {}) == {
        "timerSimpleFlg": 1,
        "timerSimpleOnoff": 1,
        "timerSimpleTime": None,
    }
    # simple_timer_time is left to the reports (255 without a timer).
    assert timer_cancel_state({}) == {"timerSimpleFlg": 0}
    assert timer_cancel_state({"timerSimpleTime": 0}) == {"timerSimpleFlg": 0, "timerSimpleTime": 0}


# ------------------------------------------------------------------------ api


async def test_set_timer_request(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """0.5 h: timerSimpleMinute 30, simple_timer_time 1, timer_switch a string."""
    answer = {"timerSimpleTime": 1790765454000, "systemTime": 1790763654000}
    aioclient_mock.post(TIMER_URL, json={**OK, "data": answer})
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.set_simple_timer(100001, CODE, 0.5, turn_on=False) == answer
    assert await api.set_simple_timer(100001, CODE, 24, turn_on=True) == answer
    first, second = (call[2] for call in aioclient_mock.mock_calls)
    assert first == {
        "timerSimpleMinute": 30,
        "ctrlJson": {"timer_switch": "0", "simple_timer_time": 1},
    }
    assert second == {
        "timerSimpleMinute": 1440,
        "ctrlJson": {"timer_switch": "1", "simple_timer_time": 48},
    }
    assert type(first["timerSimpleMinute"]) is int
    assert type(first["ctrlJson"]["simple_timer_time"]) is int


async def test_cancel_timer_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The app cancels with the same POST, never with the DELETE URL."""
    aioclient_mock.post(TIMER_URL, json={**OK, "data": {"timerSimpleTime": 0}})
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.cancel_simple_timer(100001, CODE) == {"timerSimpleTime": 0}
    (call,) = aioclient_mock.mock_calls
    assert call[0] == "POST"
    assert call[2] == {"timerSimpleMinute": 0, "ctrlJson": {"simple_timer_time": 0}}


async def test_timer_answer_without_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TIMER_URL, json=OK)
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.set_simple_timer(100001, CODE, 1, turn_on=True) == {}


async def test_timer_rejected(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TIMER_URL, json={"code": "500", "message": "设备不在线"})
    api = _api(hass, auth=Session("tok", 200002))
    with pytest.raises(YorkCommandError, match="设备不在线"):
        await api.set_simple_timer(100001, CODE, 1, turn_on=True)


async def test_timer_duration_checked(hass: HomeAssistant) -> None:
    api = _api(hass, auth=Session("tok", 200002))
    for hours in (0, 0.25, 10.5, 25):
        with pytest.raises(ValueError):
            await api.set_simple_timer(100001, CODE, hours, turn_on=True)


async def test_timer_reclaims_session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Setting a timer is an explicit action, like a command."""
    aioclient_mock.post(TIMER_URL, side_effect=_sequence(UNAUTHORIZED, OK))
    aioclient_mock.get(f"{API_BASE}/login", json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("old", 200002))
    await api.cancel_simple_timer(100001, CODE)
    assert api.auth.token == "fresh"


# ---------------------------------------------------------------- entities


async def test_timer_entities_from_fixture(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    select = _select(hass)
    assert select.state == "off"
    assert select.attributes[ATTR_OPTIONS] == ["off", *TIMER_OPTIONS]
    # The unit runs: a new timer would switch it off.
    assert select.attributes["next_action"] == "off"
    assert "action" not in select.attributes
    end = _end(hass)
    assert end.state == STATE_UNKNOWN
    assert end.attributes["device_class"] == "timestamp"
    assert "action" not in end.attributes


async def test_timer_entities_need_the_fields(hass: HomeAssistant, mock_api, config_entry) -> None:
    status = {
        k: v
        for k, v in FIXTURE["status"].items()
        if k not in ("timerSimpleFlg", "timerSimpleOnoff", "timerSimpleTime")
    }
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    assert _entity(hass, SELECT_DOMAIN, "simple_timer") is None
    assert _entity(hass, "sensor", "timer_end") is None
    # Other sensors are still there.
    assert _entity(hass, "sensor", "indoor_temperature") is not None


async def test_timer_set_elsewhere(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A timer from the app or panel: end known, duration unknown."""
    status = _timer_status(3, turns_on=True, if_run="0")
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    end = _end(hass)
    assert datetime.fromisoformat(end.state).timestamp() * 1000 == pytest.approx(
        status["timerSimpleTime"], abs=1000
    )
    assert end.attributes["action"] == "on"
    select = _select(hass)
    assert select.state == STATE_UNKNOWN  # simple_timer_time 255: duration unknown
    assert select.attributes["action"] == "on"
    assert select.attributes["next_action"] == "on"

    # The app never reads simple_timer_time back (it may count down):
    # a value matching a duration is not shown as one.
    _with_status(mock_api, {**status, "simple_timer_time": 6})
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == STATE_UNKNOWN


async def test_timer_implausible_end(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, _timer_status(timerSimpleTime=978307200000))  # 2001
    await _setup(hass, config_entry)
    assert _end(hass).state == STATE_UNKNOWN
    assert _select(hass).state == STATE_UNKNOWN


async def test_set_timer_turns_running_unit_off(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    end_ms = _now_ms() + 2 * HOUR_MS
    mock_api.set_simple_timer.return_value = {"timerSimpleTime": end_ms, "systemTime": _now_ms()}
    mock_api.get_status.reset_mock()
    await _choose(hass, "2h")
    # The power state decides the action: the full state is read first.
    assert mock_api.get_status.call_args_list[0].args[1] == [(CODE, 0)]
    mock_api.set_simple_timer.assert_awaited_once_with(
        coordinator.devices[CODE].home_id, CODE, 2.0, turn_on=False
    )
    mock_api.control.assert_not_called()
    select = _select(hass)
    assert select.state == "2h"
    assert select.attributes["action"] == "off"
    end = _end(hass)
    assert datetime.fromisoformat(end.state).timestamp() * 1000 == pytest.approx(end_ms, abs=1000)
    assert end.attributes["action"] == "off"
    # The cloud's own fields are read in full once the grace period is over.
    assert coordinator.devices[CODE].recheck_after is not None


async def test_set_timer_turns_stopped_unit_on(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, _status(if_run="0"))
    await _setup(hass, config_entry)
    assert _select(hass).attributes["next_action"] == "on"
    await _choose(hass, "0_5h")
    args = mock_api.set_simple_timer.call_args
    assert args.args[1:] == (CODE, 0.5) and args.kwargs == {"turn_on": True}
    select = _select(hass)
    assert select.state == "0_5h"
    assert select.attributes["action"] == "on"
    # No time in the answer: the end stays unknown until the cloud reports it.
    assert _end(hass).state == STATE_UNKNOWN


async def test_set_timer_uses_fresh_power_state(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The unit was switched off on the panel since the last poll."""
    await _setup(hass, config_entry)
    _with_status(mock_api, _status(if_run="0"))
    await _choose(hass, "1h")
    assert mock_api.set_simple_timer.call_args.kwargs == {"turn_on": True}


async def test_cancel_timer(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, _timer_status(5))
    await _setup(hass, config_entry)
    assert _select(hass).state == STATE_UNKNOWN
    await _choose(hass, "off")
    mock_api.cancel_simple_timer.assert_awaited_once()
    assert mock_api.cancel_simple_timer.call_args.args[1] == CODE
    assert _select(hass).state == "off"
    assert _end(hass).state == STATE_UNKNOWN


async def test_cancel_without_timer_sends_nothing(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The page returns early when hasTimer is false."""
    await _setup(hass, config_entry)
    await _choose(hass, "off")
    mock_api.cancel_simple_timer.assert_not_called()
    mock_api.set_simple_timer.assert_not_called()


async def test_timer_from_ha_then_replaced_elsewhere(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The remembered duration only applies while the cloud reports that timer."""
    await _setup(hass, config_entry)
    end_ms = _now_ms() + HOUR_MS
    mock_api.set_simple_timer.return_value = {"timerSimpleTime": end_ms}
    await _choose(hass, "1h")
    assert _select(hass).state == "1h"
    # Grace over; the app has set a 4 h timer meanwhile.
    _with_status(mock_api, _timer_status(4))
    with patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + 60,
    ):
        await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("overrides", "key"),
    [
        ({"otaFlg": "1"}, "ota_in_progress"),
        ({"self_clean": 2}, "cooling_down"),
    ],
)
async def test_timer_refused(hass: HomeAssistant, mock_api, config_entry, overrides, key) -> None:
    _with_status(mock_api, _timer_status(**overrides))
    await _setup(hass, config_entry)
    for option in ("1h", "off"):
        with pytest.raises(HomeAssistantError) as info:
            await _choose(hass, option)
        assert info.value.translation_key == key
    mock_api.set_simple_timer.assert_not_called()
    mock_api.cancel_simple_timer.assert_not_called()


async def test_timer_refused_offline(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_online.return_value = {}
    coordinator._last_online = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "unavailable"
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_set_simple_timer(CODE, 1)
    assert info.value.translation_key == "offline"
    mock_api.set_simple_timer.assert_not_called()


async def test_timer_ota_found_by_fresh_read(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    _with_status(mock_api, _status(otaFlg="1"))
    with pytest.raises(HomeAssistantError) as info:
        await _choose(hass, "1h")
    assert info.value.translation_key == "ota_in_progress"
    mock_api.set_simple_timer.assert_not_called()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (YorkCommandError("设备不在线"), "command_rejected"),
        (YorkConnectionError("boom"), "cannot_connect"),
        (YorkSessionTakenError("HTTP 401"), "session_taken"),
    ],
)
async def test_timer_errors(hass: HomeAssistant, mock_api, config_entry, error, key) -> None:
    await _setup(hass, config_entry)
    mock_api.set_simple_timer.side_effect = error
    with pytest.raises(HomeAssistantError) as info:
        await _choose(hass, "1h")
    assert info.value.translation_key == key
    # Nothing changes locally.
    assert _select(hass).state == "off"
    assert config_entry.runtime_data.devices[CODE].timer_choice is None


async def test_timer_during_yield_rereads_state(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """While the session is left to the app, the timer takes it back and re-reads first."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await coordinator.async_refresh()
    assert coordinator.session_yielded
    _with_status(mock_api, _status(if_run="0"))
    await coordinator.async_set_simple_timer(CODE, 1.5)
    mock_api.relogin.assert_awaited()
    assert mock_api.set_simple_timer.call_args.kwargs == {"turn_on": True}
    assert not coordinator.session_yielded


async def test_timer_with_extra_capabilities(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A controller with extra functions and deflectors: the timer works the same."""
    constants = {
        **FIXTURE["device"]["constantJson"],
        "is_sleep_avalable": "1",
        "is_energy_conservation_avalable": "1",
        "horizontal_air_deflector_num": "1",
        "is_humidification_avalable": "1",
    }
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: copy.deepcopy(constants)
    _with_status(mock_api, _timer_status(8, sleep="1", energy_conservation="0"))
    await _setup(hass, config_entry)
    await _choose(hass, "11h")
    args = mock_api.set_simple_timer.call_args
    assert args.args[2] == 11.0 and args.kwargs == {"turn_on": False}
    # The timer does not go through the control path: no function flags sent.
    mock_api.control.assert_not_called()
    assert _select(hass).state == "11h"
    # The answer had no time: the replaced 8 h timer's end is not shown.
    assert _end(hass).state == STATE_UNKNOWN


async def test_timer_in_diagnostics(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    status = diag["devices"][0]["status"]
    for key in (
        "timerSimpleFlg",
        "timerSimpleOnoff",
        "timerSimpleTime",
        "timer_switch",
        "simple_timer_time",
    ):
        assert key in status
    assert status["simple_timer_time"] == 255


def test_timer_fields_not_from_constants() -> None:
    """The page reads the timer only from xkqStatusListGjy[0]."""
    from custom_components.york_smart_home.coordinator import YorkDevice

    dev = YorkDevice(
        info=FIXTURE["device"],
        status={"timerSimpleFlg": 0},
        constants={"timerSimpleFlg": 1, "timerSimpleTime": 5},
    )
    assert dev.view["timerSimpleFlg"] == 0
    assert "timerSimpleTime" not in dev.view


async def test_power_change_rechecks_timer(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Switching the power with a timer running reads the timer fields in full later."""
    _with_status(mock_api, _timer_status(2))
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await coordinator.async_request_change(CODE, temperature=26)
    assert coordinator.devices[CODE].recheck_after is None
    await coordinator.async_request_change(CODE, power=False)
    assert mock_api.control.call_args.args[4]["if_run"] == "0"
    assert coordinator.devices[CODE].recheck_after is not None


async def test_power_change_without_timer_no_recheck(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await coordinator.async_request_change(CODE, power=False)
    assert coordinator.devices[CODE].recheck_after is None


async def test_fired_timer_forgets_duration(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _choose(hass, "1h")  # the answer has no time
    assert coordinator.devices[CODE].timer_choice == (None, 1.0)
    # Grace over, the cloud reports no timer (the fixture): the choice is dropped,
    # so a later timer set elsewhere does not show this duration.
    with patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + 60,
    ):
        await coordinator.async_refresh()
    assert coordinator.devices[CODE].timer_choice is None
    _with_status(mock_api, _timer_status(4))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == STATE_UNKNOWN


def _by_cursor(full: dict, incremental: dict):
    """get_status answering ``full`` for cursor 0, else ``incremental``."""

    def answer(home, codes):
        ((_, cursor),) = codes
        return {CODE: copy.deepcopy(full if cursor == 0 else incremental)}

    return answer


def _later(seconds: float):
    return patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + seconds,
    )


async def test_fired_timer_still_reported(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The cloud still has the flag for an end long gone: no past time, no duration."""
    _with_status(mock_api, _timer_status(-0.5))
    await _setup(hass, config_entry)
    assert _end(hass).state == STATE_UNKNOWN
    select = _select(hass)
    assert select.state == STATE_UNKNOWN
    assert "action" not in select.attributes


async def test_timer_end_triggers_full_read(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A timer firing on its own is read in full soon after its end.

    The incremental reports leave the timer fields out; without the full
    read HA would show the timer until the next discovery.
    """
    status = _timer_status(10 / 3600)  # fires in 10 s
    report = {k: v for k, v in status.items() if not k.startswith("timerSimple")}
    mock_api.get_status.side_effect = _by_cursor(status, report)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    dev = coordinator.devices[CODE]
    assert dev.timer_check_end == status["timerSimpleTime"]
    assert dev.timer_check_after is not None
    assert _end(hass).state != STATE_UNKNOWN

    # Before the check is due: incremental reads only.
    mock_api.get_status.reset_mock()
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] != [(CODE, 0)]
    assert timer_active(dev.status)

    # The timer has fired: the full state now has no timer.
    mock_api.get_status.side_effect = _by_cursor(_status(if_run="0"), report)
    mock_api.get_status.reset_mock()
    with _later(10 + 61):
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
    assert dev.timer_check_after is None
    await hass.async_block_till_done()
    assert _select(hass).state == "off"
    assert _end(hass).state == STATE_UNKNOWN

    # Checked once per end: no more full reads for it.
    mock_api.get_status.reset_mock()
    with _later(10 + 200):
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] != [(CODE, 0)]


async def test_timer_check_once_while_cloud_keeps_flag(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """If the full read still shows the fired timer, it is not re-read on every poll."""
    status = _timer_status(10 / 3600)
    report = {k: v for k, v in status.items() if not k.startswith("timerSimple")}
    mock_api.get_status.side_effect = _by_cursor(status, report)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    with _later(10 + 61):
        await coordinator.async_refresh()
        assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
        mock_api.get_status.reset_mock()
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] != [(CODE, 0)]


async def test_recheck_after_set_reads_cloud_timer(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """After the grace period the cloud's own timer fields replace the local ones."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.set_simple_timer.return_value = {"timerSimpleTime": _now_ms() + 2 * HOUR_MS}
    await _choose(hass, "2h")
    assert _select(hass).state == "2h"
    cloud = _timer_status(1.5, turns_on=True)
    report = {k: v for k, v in FIXTURE["status"].items() if not k.startswith("timerSimple")}
    mock_api.get_status.side_effect = _by_cursor(cloud, report)
    mock_api.get_status.reset_mock()
    with _later(COMMAND_GRACE + 1):
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
    await hass.async_block_till_done()
    end = _end(hass)
    assert datetime.fromisoformat(end.state).timestamp() * 1000 == pytest.approx(
        cloud["timerSimpleTime"], abs=1000
    )
    assert end.attributes["action"] == "on"
    select = _select(hass)
    assert select.attributes["action"] == "on"
    # Not the timer set from HA any more: its duration is not known.
    assert select.state == STATE_UNKNOWN


async def test_timer_without_answer_time_pinned(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """An answer without a time: the duration follows the first reported end only."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _choose(hass, "1h")  # the answer has no time
    assert _select(hass).state == "1h"
    assert _end(hass).state == STATE_UNKNOWN
    first = _timer_status(1)
    _with_status(mock_api, first)
    with _later(COMMAND_GRACE + 1):
        await coordinator.async_refresh()
    assert coordinator.devices[CODE].timer_choice == (first["timerSimpleTime"], 1.0)
    await hass.async_block_till_done()
    assert _select(hass).state == "1h"
    # Replaced in the app: another end, so the HA duration no longer applies.
    _with_status(mock_api, _timer_status(4))
    with _later(COMMAND_GRACE + 2):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == STATE_UNKNOWN


async def test_answer_end_differs_slightly_from_report(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Seen live: the answer's end is the cloud's, the report's the controller's (~3 s later).

    The 10 h timer set from HA must keep showing 10 h, not unknown.
    """
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    answer_end = _now_ms() + 10 * HOUR_MS
    mock_api.set_simple_timer.return_value = {"timerSimpleTime": answer_end}
    await _choose(hass, "10h")
    assert _select(hass).state == "10h"
    reported = _status(timerSimpleFlg=1, timerSimpleOnoff=0, timerSimpleTime=answer_end + 2842)
    _with_status(mock_api, reported)
    with _later(COMMAND_GRACE + 1):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.devices[CODE].timer_choice == (answer_end + 2842, 10.0)
    assert _select(hass).state == "10h"
    end = _end(hass)
    assert datetime.fromisoformat(end.state).timestamp() * 1000 == pytest.approx(
        answer_end + 2842, abs=1000
    )
    # A timer replaced elsewhere with another duration still shows unknown.
    _with_status(mock_api, _timer_status(9.5))
    with _later(COMMAND_GRACE + 2):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == STATE_UNKNOWN


async def test_unsupported_duration(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.reset_mock()
    with pytest.raises(ServiceValidationError) as info:
        await coordinator.async_set_simple_timer(CODE, 10.5)
    assert info.value.translation_key == "unsupported_timer"
    assert info.value.translation_placeholders == {"option": "10.5h"}
    with pytest.raises(ServiceValidationError) as info:
        await YorkSimpleTimer(coordinator, CODE).async_select_option("10_5h")
    assert info.value.translation_key == "unsupported_timer"
    mock_api.set_simple_timer.assert_not_called()
    mock_api.get_status.assert_not_called()  # refused before reading
