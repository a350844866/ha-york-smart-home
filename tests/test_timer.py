"""Simple timer (简易定时): API requests, state helpers, select and end sensor."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import itertools
import json
from pathlib import Path
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
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
    FAILURES_BEFORE_UNAVAILABLE,
    TIMER_HOURS,
)
from custom_components.york_smart_home.control import (
    TIMER_FIRED_MS,
    TIMER_OPTIONS,
    has_simple_timer,
    new_timer_turns_on,
    timer_active,
    timer_cancel_state,
    timer_end_ms,
    timer_end_text,
    timer_end_time,
    timer_fired,
    timer_fired_at,
    timer_option,
    timer_pending,
    timer_set_state,
    timer_text_changes_at,
    timer_turns_on,
)
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.select import YorkSimpleTimer
from homeassistant.components.select import (
    ATTR_CYCLE,
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_NEXT,
    SERVICE_SELECT_OPTION,
    SERVICE_SELECT_PREVIOUS,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CORE_CONFIG_UPDATE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.icon import async_get_icons
from homeassistant.helpers.state import async_reproduce_state
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import get_scheduled_timer_handles

from .conftest import CODE, FIXTURE
from .test_api import OK, UNAUTHORIZED, _api, _login_body, _sequence

TIMER_URL = f"{API_BASE}/api/appdevice/homes/100001/xkqs/{CODE}/simpleTimer"
HOUR_MS = 3600 * 1000
# Just over TIMER_FIRED_MS after its end a timer counts as fired.
FIRED = timedelta(milliseconds=TIMER_FIRED_MS + 1)

# Home Assistant's time zone in these tests, and the clock every test starts
# at: 2026-10-01 10:00 in Shanghai (02:00 UTC).
TIME_ZONE = "Asia/Shanghai"
SHANGHAI = ZoneInfo(TIME_ZONE)


def _local(*args: int) -> datetime:
    """A time in Shanghai: _local(2026, 10, 1, 12, 22)."""
    return datetime(*args, tzinfo=SHANGHAI)


NOW = _local(2026, 10, 1, 10, 0)


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory) -> None:
    """A fixed clock: the timer texts name the day relative to it."""
    freezer.move_to(NOW)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _status(**overrides) -> dict:
    return {**FIXTURE["status"], **overrides}


def _timer(end, *, turns_on: bool = False, flag=1) -> dict:
    """Timer fields as reported; a datetime end is sent as epoch ms."""
    return {
        "timerSimpleFlg": flag,
        "timerSimpleOnoff": 1 if turns_on else 0,
        "timerSimpleTime": _ms(end) if isinstance(end, datetime) else end,
    }


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


async def _setup(hass: HomeAssistant, entry, *, language: str = "en", polling: bool = True) -> None:
    """Set up in Shanghai time; ``polling=False`` is the entry's "Enable polling" off."""
    await hass.config.async_set_time_zone(TIME_ZONE)
    hass.config.language = language
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    if not polling:
        hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: datetime) -> None:
    """Move the clock to ``moment`` and run the timers due by then."""
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _entity(hass, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{CODE}_{key}")


def _select(hass):
    return hass.states.get(_entity(hass, SELECT_DOMAIN, "simple_timer"))


def _end(hass):
    return hass.states.get(_entity(hass, "sensor", "timer_end"))


def _clock_writes(hass: HomeAssistant) -> dict[str, datetime]:
    """Pending state writes at a clock change: entity id -> when.

    The handles async_track_point_in_utc_time schedules for the entities'
    _clock_write.
    """
    writes = {}
    for handle in get_scheduled_timer_handles(hass.loop):
        track = handle._callback
        target = getattr(getattr(track, "job", None), "target", None)
        if not handle.cancelled() and getattr(target, "__name__", "") == "_clock_write":
            writes[target.__self__.entity_id] = track.utc_point_in_time
    return writes


async def _choose(hass, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: _entity(hass, SELECT_DOMAIN, "simple_timer"), ATTR_OPTION: option},
        blocking=True,
    )


def _later(seconds: float):
    return patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + seconds,
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
    """The author's controller: timerSimpleFlg null, timerSimpleTime 0, 255."""
    status = FIXTURE["status"]
    assert has_simple_timer(status)
    assert not timer_active(status)
    assert timer_end_ms(status, _now_ms()) is None
    assert not timer_fired(status, _now_ms())
    assert not timer_pending(status, _now_ms())
    assert timer_end_text(status, NOW, chinese=True) == "无定时"
    assert timer_end_text(status, NOW, chinese=False) == "No timer"
    assert not has_simple_timer({"if_run": "1"})


def test_timer_state_helpers() -> None:
    now = _now_ms()
    status = _status(timerSimpleFlg=1, timerSimpleOnoff=1, timerSimpleTime=now + HOUR_MS)
    assert timer_active(status) and timer_turns_on(status)
    assert timer_pending(status, now)
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
    assert not timer_pending({**status, "timerSimpleFlg": 0}, now)


def test_timer_end_in_the_past() -> None:
    """Just past: still shown (the cloud may lag); over 10 minutes: fired."""
    now = _now_ms()
    status = _status(timerSimpleFlg=1, timerSimpleOnoff=0)
    just = {**status, "timerSimpleTime": now - 5 * 60 * 1000}
    assert timer_end_ms(just, now) == now - 5 * 60 * 1000
    assert not timer_fired(just, now)
    assert timer_pending(just, now)
    gone = {**status, "timerSimpleTime": now - 11 * 60 * 1000}
    assert timer_end_ms(gone, now) is None
    assert timer_fired(gone, now)
    assert not timer_pending(gone, now)
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


# ----------------------------------------------------------------- end text


@pytest.mark.parametrize(
    ("now", "end", "turns_on", "chinese", "english"),
    [
        # Today: 24-hour HH:MM with leading zeros, and the action.
        (NOW, _local(2026, 10, 1, 12, 22), False, "12:22 关机", "Off at 12:22"),
        (
            _local(2026, 10, 1, 7, 5),
            _local(2026, 10, 1, 8, 0, 59),
            True,
            "08:00 开机",
            "On at 08:00",
        ),
        # Just past, not fired yet (the cloud may lag).
        (NOW, _local(2026, 10, 1, 9, 51), False, "09:51 关机", "Off at 09:51"),
        # Tomorrow.
        (
            _local(2026, 10, 1, 22, 0),
            _local(2026, 10, 2, 8, 0),
            True,
            "明天 08:00 开机",
            "On at 08:00 tomorrow",
        ),
        # Yesterday: within the 10 minutes after the end, across midnight.
        (
            _local(2026, 10, 2, 0, 5),
            _local(2026, 10, 1, 23, 58),
            False,
            "昨天 23:58 关机",
            "Off at 23:58 yesterday",
        ),
        # Any other day: only within the 25 h window.
        (
            _local(2026, 10, 1, 23, 30),
            _local(2026, 10, 3, 0, 30),
            False,
            "10月3日 00:30 关机",
            "Off at 00:30 on Oct 3",
        ),
        (
            _local(2026, 12, 31, 23, 50),
            _local(2027, 1, 2, 0, 15),
            True,
            "1月2日 00:15 开机",
            "On at 00:15 on Jan 2",
        ),
    ],
)
def test_timer_end_text(now, end, turns_on, chinese, english) -> None:
    status = _timer(end, turns_on=turns_on)
    assert timer_end_text(status, now, chinese=True) == chinese
    assert timer_end_text(status, now, chinese=False) == english
    assert timer_end_time(status, now) == end


@pytest.mark.parametrize(
    ("status", "chinese", "english"),
    [
        ({}, "无定时", "No timer"),
        (_timer(None, flag=None), "无定时", "No timer"),
        # Cancelled: an end without the flag.
        (_timer(NOW + timedelta(hours=1), flag=0), "无定时", "No timer"),
        # Fired: over 10 minutes past, or a nonsense end in the past.
        (_timer(NOW - timedelta(minutes=11)), "无定时", "No timer"),
        (_timer(978307200000), "无定时", "No timer"),
        # Set, but the end is not known: none yet, not a number, too far ahead.
        (_timer(None), "已设定定时", "Timer set"),
        (_timer("soon", turns_on=True), "已设定定时", "Timer set"),
        (_timer(NOW + timedelta(hours=26)), "已设定定时", "Timer set"),
    ],
)
def test_timer_end_text_without_a_time(status, chinese, english) -> None:
    assert timer_end_text(status, NOW, chinese=True) == chinese
    assert timer_end_text(status, NOW, chinese=False) == english
    assert timer_end_time(status, NOW) is None


def test_timer_end_text_in_the_time_zone() -> None:
    """The same instants named in Shanghai and in UTC."""
    status = _timer(_local(2026, 10, 2, 0, 30))  # 16:30 UTC on Oct 1
    assert timer_end_text(status, NOW, chinese=False) == "Off at 00:30 tomorrow"
    assert timer_end_text(status, NOW.astimezone(UTC), chinese=False) == "Off at 16:30"
    assert timer_end_time(status, NOW).isoformat() == "2026-10-02T00:30:00+08:00"
    assert timer_end_time(status, NOW.astimezone(UTC)).isoformat() == "2026-10-01T16:30:00+00:00"


def test_timer_end_text_month_names() -> None:
    """Fixed English abbreviations, whatever the locale."""
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for month, name in enumerate(names, start=1):
        now = _local(2026, month, 10, 23, 30)
        status = _timer(now + timedelta(hours=24, minutes=50))  # 00:20 on the 12th
        assert timer_end_text(status, now, chinese=False) == f"Off at 00:20 on {name} 12"
        assert timer_end_text(status, now, chinese=True) == f"{month}月12日 00:20 关机"


def test_timer_text_changes_at() -> None:
    """The moments the clock alone changes the text: midnight, fired, in the window."""
    midnight = _local(2026, 10, 2, 0, 0)
    # Today: next, it counts as fired.
    end = _local(2026, 10, 1, 12, 22)
    assert timer_text_changes_at(_timer(end), NOW) == end + FIRED
    assert timer_fired_at(_timer(end), NOW) == end + FIRED
    # Tomorrow: today from midnight on.
    late = _local(2026, 10, 1, 23, 30)
    tomorrow = _timer(_local(2026, 10, 2, 8, 0), turns_on=True)
    assert timer_text_changes_at(tomorrow, late) == midnight
    assert timer_end_text(tomorrow, midnight - timedelta(microseconds=1), True) == "明天 08:00 开机"
    assert timer_end_text(tomorrow, midnight, True) == "08:00 开机"
    # Across midnight: yesterday from midnight on, then fired.
    end = _local(2026, 10, 1, 23, 55)
    before = _local(2026, 10, 1, 23, 50)
    assert timer_text_changes_at(_timer(end), before) == midnight
    assert timer_text_changes_at(_timer(end), midnight) == end + FIRED
    assert timer_fired_at(_timer(end), before) == end + FIRED
    almost = end + FIRED - timedelta(milliseconds=1)
    assert timer_end_text(_timer(end), almost, False) == "Off at 23:55 yesterday"
    assert timer_end_text(_timer(end), end + FIRED, False) == "No timer"
    # Implausibly far ahead: shown once within 25 h.
    far = _timer(NOW + timedelta(hours=26))
    assert timer_text_changes_at(far, NOW) == NOW + timedelta(hours=1)
    assert timer_end_text(far, NOW + timedelta(hours=1), False) == "Off at 12:00 tomorrow"
    # Nothing changes by itself.
    for status in (
        {},
        FIXTURE["status"],
        _timer(None),
        _timer("soon"),
        _timer(end, flag=0),
        _timer(NOW - timedelta(minutes=11)),
    ):
        assert timer_text_changes_at(status, NOW) is None
        assert timer_fired_at(status, NOW) is None
    # Beyond datetime's range: no moment, no error.
    assert timer_text_changes_at(_timer(1e20), NOW) is None
    assert timer_fired_at(_timer(1e20), NOW) is None


def test_timer_text_changes_at_repeated_midnight() -> None:
    """Clocks set back across midnight (St. John's until 2011): the midnight still ahead.

    On 2010-11-07 at 00:01 NDT the clocks went back to 23:01 NST on the 6th,
    so 23:30 on the 6th came twice. Never a midnight already past, which
    would come round again at once.
    """
    tz = ZoneInfo("America/St_Johns")
    status = _timer(datetime(2010, 11, 7, 8, 0, tzinfo=tz))
    first = datetime(2010, 11, 6, 23, 30, tzinfo=tz)
    second = datetime(2010, 11, 6, 23, 30, fold=1, tzinfo=tz)
    assert second - first == timedelta(0)  # the same wall time ...
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)  # ... twice
    assert timer_text_changes_at(status, first) == datetime(2010, 11, 7, 2, 30, tzinfo=UTC)
    assert timer_text_changes_at(status, second) == datetime(2010, 11, 7, 3, 30, tzinfo=UTC)
    assert timer_end_text(status, second, chinese=False) == "Off at 08:00 tomorrow"


@pytest.mark.parametrize(
    ("chinese", "texts"),
    [
        (True, ("明天 00:00 关机", "00:00 关机", "无定时")),
        (False, ("Off at 00:00 tomorrow", "Off at 00:00", "No timer")),
    ],
)
def test_timer_end_at_midnight(chinese: bool, texts: tuple[str, str, str]) -> None:
    """An end exactly at local midnight: tomorrow up to it, today from it on, then fired."""
    midnight = _local(2026, 10, 2, 0, 0)
    status = _timer(midnight)
    last = midnight - timedelta(microseconds=1)  # 23:59:59.999999
    assert timer_end_text(status, last, chinese) == texts[0]
    assert timer_text_changes_at(status, last) == midnight
    assert timer_end_text(status, midnight, chinese) == texts[1]
    assert timer_text_changes_at(status, midnight) == midnight + FIRED
    assert timer_end_text(status, midnight + FIRED - timedelta(milliseconds=1), chinese) == texts[1]
    assert timer_end_text(status, midnight + FIRED, chinese) == texts[2]
    assert timer_text_changes_at(status, midnight + FIRED) is None
    assert timer_end_time(status, midnight).isoformat() == "2026-10-02T00:00:00+08:00"


@pytest.mark.parametrize(
    ("path", "running", "end_time", "error"),
    [
        (
            "strings.json",
            "Running (duration unknown)",
            "End time",
            "The running timer's duration is unknown: choose a duration or Off",
        ),
        (
            "translations/zh-Hans.json",
            "定时中（时长未知）",
            "结束时间",
            "定时时长未知，请选择具体时长或「关闭定时」",
        ),
    ],
)
def test_timer_translations(path: str, running: str, end_time: str, error: str) -> None:
    base = Path(__file__).parent.parent / "custom_components" / DOMAIN
    strings = json.loads((base / path).read_text("utf-8"))
    states = strings["entity"]["select"]["simple_timer"]["state"]
    assert list(states) == ["off", "running", *TIMER_OPTIONS]
    assert states["running"] == running
    attributes = strings["entity"]["sensor"]["timer_end"]["state_attributes"]
    assert attributes["end_time"]["name"] == end_time
    assert set(attributes["action"]["state"]) == {"on", "off"}
    assert strings["exceptions"]["timer_duration_unknown"]["message"] == error


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
    assert end.state == "No timer"
    # A text: no device class, state class or unit.
    for key in ("device_class", "state_class", "unit_of_measurement", "action", "end_time"):
        assert key not in end.attributes, key
    # Nothing to change with the clock.
    assert _clock_writes(hass) == {}


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


async def test_timer_end_was_a_timestamp(hass: HomeAssistant, mock_api, config_entry) -> None:
    """0.5.x registered 定时结束 as a timestamp sensor: same entity, now a text."""
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{CODE}_timer_end",
        config_entry=config_entry,
        original_device_class=SensorDeviceClass.TIMESTAMP,
        suggested_object_id="york_timer_end",
    )
    _with_status(mock_api, _timer_status(3))
    await _setup(hass, config_entry)
    assert _entity(hass, "sensor", "timer_end") == old.entity_id
    entry = registry.async_get(old.entity_id)
    assert entry.original_device_class is None
    assert entry.device_class is None
    state = hass.states.get(old.entity_id)
    assert state.state == "Off at 13:00"
    assert "device_class" not in state.attributes


async def test_timer_end_icon(hass: HomeAssistant) -> None:
    """A timer icon (icons.json): without the timestamp class it would be the generic one."""
    icons = await async_get_icons(hass, "entity", integrations=[DOMAIN])
    assert icons == {DOMAIN: {"sensor": {"timer_end": {"default": "mdi:timer-outline"}}}}


async def test_timer_set_elsewhere(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A timer from the app or panel: end known, duration unknown ("running")."""
    status = _timer_status(3, turns_on=True, if_run="0")
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    end = _end(hass)
    assert end.state == "On at 13:00"
    assert end.attributes["end_time"] == "2026-10-01T13:00:00+08:00"
    assert (
        datetime.fromisoformat(end.attributes["end_time"]).timestamp() * 1000
        == (status["timerSimpleTime"])
    )
    assert end.attributes["action"] == "on"
    select = _select(hass)
    assert select.state == "running"  # simple_timer_time 255: duration unknown
    assert select.attributes[ATTR_OPTIONS] == ["off", "running", *TIMER_OPTIONS]
    assert select.attributes["action"] == "on"
    assert select.attributes["next_action"] == "on"

    # The app never reads simple_timer_time back (it may count down):
    # a value matching a duration is not shown as one.
    _with_status(mock_api, {**status, "simple_timer_time": 6})
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "running"


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("zh-Hans", "13:00 开机"),
        ("zh-Hant", "13:00 开机"),
        ("en", "On at 13:00"),
        ("de", "On at 13:00"),
    ],
)
async def test_timer_end_in_ha_language(
    hass: HomeAssistant, mock_api, config_entry, language: str, text: str
) -> None:
    """Chinese for every zh* language of Home Assistant, else English."""
    _with_status(mock_api, _timer_status(3, turns_on=True, if_run="0"))
    await _setup(hass, config_entry, language=language)
    end = _end(hass)
    assert end.state == text
    assert end.attributes == {
        **end.attributes,
        "end_time": "2026-10-01T13:00:00+08:00",
        "action": "on",
    }
    # The select's state is the option id (its name is translated by HA).
    assert _select(hass).state == "running"


@pytest.mark.parametrize(
    ("end", "end_text", "select_state"),
    [
        # 2001: over 10 minutes past, so fired.
        (978307200000, "No timer", "off"),
        # 26 h ahead: set, but the end cannot be right.
        (_ms(NOW + timedelta(hours=26)), "Timer set", "running"),
    ],
)
async def test_timer_implausible_end(
    hass: HomeAssistant, mock_api, config_entry, end, end_text, select_state
) -> None:
    _with_status(mock_api, _timer_status(timerSimpleTime=end))
    await _setup(hass, config_entry)
    state = _end(hass)
    assert state.state == end_text
    assert "end_time" not in state.attributes
    assert ("action" in state.attributes) is (select_state == "running")
    assert _select(hass).state == select_state


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
    assert select.attributes[ATTR_OPTIONS] == ["off", *TIMER_OPTIONS]
    assert select.attributes["action"] == "off"
    end = _end(hass)
    assert end.state == "Off at 12:00"
    assert end.attributes["end_time"] == "2026-10-01T12:00:00+08:00"
    assert end.attributes["action"] == "off"
    # The cloud's own fields are read in full once the grace period is over.
    assert coordinator.devices[CODE].recheck_after is not None


async def test_set_timer_turns_stopped_unit_on(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, _status(if_run="0"))
    await _setup(hass, config_entry, language="zh-Hans")
    assert _select(hass).attributes["next_action"] == "on"
    await _choose(hass, "0_5h")
    args = mock_api.set_simple_timer.call_args
    assert args.args[1:] == (CODE, 0.5) and args.kwargs == {"turn_on": True}
    select = _select(hass)
    assert select.state == "0_5h"
    assert select.attributes["action"] == "on"
    # No time in the answer: set, the end unknown until the cloud reports it.
    end = _end(hass)
    assert end.state == "已设定定时"
    assert end.attributes["action"] == "on"
    assert "end_time" not in end.attributes


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
    assert _select(hass).state == "running"
    await _choose(hass, "off")
    mock_api.cancel_simple_timer.assert_awaited_once()
    assert mock_api.cancel_simple_timer.call_args.args[1] == CODE
    select = _select(hass)
    assert select.state == "off"
    assert select.attributes[ATTR_OPTIONS] == ["off", *TIMER_OPTIONS]
    end = _end(hass)
    assert end.state == "No timer"
    assert "action" not in end.attributes and "end_time" not in end.attributes


async def test_cancel_without_timer_sends_nothing(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The page returns early when hasTimer is false."""
    await _setup(hass, config_entry)
    await _choose(hass, "off")
    mock_api.cancel_simple_timer.assert_not_called()
    mock_api.set_simple_timer.assert_not_called()


async def test_running_listed_only_while_current(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Without a pending timer, HA's own validation refuses "running"."""
    await _setup(hass, config_entry)
    assert "running" not in _select(hass).attributes[ATTR_OPTIONS]
    mock_api.get_status.reset_mock()
    with pytest.raises(ServiceValidationError) as info:
        await _choose(hass, "running")
    assert info.value.translation_domain == SELECT_DOMAIN
    assert info.value.translation_key == "not_valid_option"
    mock_api.get_status.assert_not_called()
    mock_api.set_simple_timer.assert_not_called()
    mock_api.cancel_simple_timer.assert_not_called()


async def test_select_running_changes_nothing(hass: HomeAssistant, mock_api, config_entry) -> None:
    """While it is the state (e.g. an automation selects it again): no request at all."""
    _with_status(mock_api, _timer_status(3))
    await _setup(hass, config_entry)
    select = _select(hass)
    assert select.state == "running"
    # The registry's capabilities follow the listed options.
    entry = er.async_get(hass).async_get(select.entity_id)
    assert entry.capabilities[ATTR_OPTIONS] == ["off", "running", *TIMER_OPTIONS]
    mock_api.get_status.reset_mock()
    await _choose(hass, "running")
    mock_api.get_status.assert_not_called()
    mock_api.set_simple_timer.assert_not_called()
    mock_api.cancel_simple_timer.assert_not_called()
    assert _select(hass).state == "running"

    # Cancelled in the app: "running" is no longer listed.
    _with_status(mock_api, _status())
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "off"
    entry = er.async_get(hass).async_get(select.entity_id)
    assert entry.capabilities[ATTR_OPTIONS] == ["off", *TIMER_OPTIONS]


async def test_scene_restore_never_selects_running(
    hass: HomeAssistant, mock_api, config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """HA's scene restore skips "running" while it is the state, and refuses it once not listed."""
    _with_status(mock_api, _timer_status(3))
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SELECT_DOMAIN, "simple_timer")
    with patch.object(YorkSimpleTimer, "async_select_option") as select_option:
        await async_reproduce_state(hass, State(entity_id, "running"))
        assert _select(hass).state == "running"
        _with_status(mock_api, _status())  # cancelled in the app
        await config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _select(hass).state == "off"
        await async_reproduce_state(hass, State(entity_id, "running"))
        select_option.assert_not_called()
    assert f"Invalid state specified for {entity_id}: running" in caplog.text
    assert _select(hass).state == "off"


async def test_select_running_no_longer_current(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Not the state when handled (a direct call; HA refuses it unlisted): a translated error."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_status.reset_mock()
    with pytest.raises(ServiceValidationError) as info:
        await YorkSimpleTimer(coordinator, CODE).async_select_option("running")
    assert info.value.translation_domain == DOMAIN
    assert info.value.translation_key == "timer_duration_unknown"
    mock_api.get_status.assert_not_called()
    mock_api.set_simple_timer.assert_not_called()
    mock_api.cancel_simple_timer.assert_not_called()


@pytest.mark.parametrize(
    ("service", "expected"),
    [(SERVICE_SELECT_PREVIOUS, "off"), (SERVICE_SELECT_NEXT, "0_5h")],
)
async def test_previous_and_next_from_running(
    hass: HomeAssistant, mock_api, config_entry, service: str, expected: str
) -> None:
    """HA's previous / next step from "running" to off / the shortest duration."""
    _with_status(mock_api, _timer_status(3))
    await _setup(hass, config_entry)
    await hass.services.async_call(
        SELECT_DOMAIN,
        service,
        {ATTR_ENTITY_ID: _entity(hass, SELECT_DOMAIN, "simple_timer"), ATTR_CYCLE: False},
        blocking=True,
    )
    if expected == "off":
        mock_api.cancel_simple_timer.assert_awaited_once()
        mock_api.set_simple_timer.assert_not_called()
    else:
        mock_api.cancel_simple_timer.assert_not_called()
        args = mock_api.set_simple_timer.call_args
        assert args.args[1:] == (CODE, 0.5) and args.kwargs == {"turn_on": False}
    assert _select(hass).state == expected


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
    with _later(60):
        await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "running"
    assert _end(hass).state == "Off at 14:00"


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
    assert _select(hass).state == STATE_UNAVAILABLE
    assert _end(hass).state == STATE_UNAVAILABLE
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
    assert _end(hass).state == "No timer"
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
    assert _end(hass).state == "Off at 18:00"
    await _choose(hass, "11h")
    args = mock_api.set_simple_timer.call_args
    assert args.args[2] == 11.0 and args.kwargs == {"turn_on": False}
    # The timer does not go through the control path: no function flags sent.
    mock_api.control.assert_not_called()
    assert _select(hass).state == "11h"
    # The answer had no time: the replaced 8 h timer's end is not shown.
    end = _end(hass)
    assert end.state == "Timer set"
    assert "end_time" not in end.attributes


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
    with _later(60):
        await coordinator.async_refresh()
    assert coordinator.devices[CODE].timer_choice is None
    _with_status(mock_api, _timer_status(4))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "running"


def _by_cursor(full: dict, incremental: dict):
    """get_status answering ``full`` for cursor 0, else ``incremental``."""

    def answer(home, codes):
        ((_, cursor),) = codes
        return {CODE: copy.deepcopy(full if cursor == 0 else incremental)}

    return answer


async def test_fired_timer_still_reported(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The cloud still has the flag for an end long gone: no timer, no past time."""
    _with_status(mock_api, _timer_status(-0.5))
    await _setup(hass, config_entry)
    end = _end(hass)
    assert end.state == "No timer"
    assert "action" not in end.attributes and "end_time" not in end.attributes
    select = _select(hass)
    assert select.state == "off"
    assert select.attributes[ATTR_OPTIONS] == ["off", *TIMER_OPTIONS]
    assert "action" not in select.attributes
    assert select.attributes["next_action"] == "off"
    assert _clock_writes(hass) == {}


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
    assert _end(hass).state == "Off at 10:00"

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
    assert _end(hass).state == "No timer"

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
    assert end.state == "On at 11:30"
    assert end.attributes["end_time"] == "2026-10-01T11:30:00+08:00"
    assert end.attributes["action"] == "on"
    select = _select(hass)
    assert select.attributes["action"] == "on"
    # Not the timer set from HA any more: its duration is not known.
    assert select.state == "running"


async def test_timer_without_answer_time_pinned(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """An answer without a time: the duration follows the first reported end only."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _choose(hass, "1h")  # the answer has no time
    assert _select(hass).state == "1h"
    assert _end(hass).state == "Timer set"
    first = _timer_status(1)
    _with_status(mock_api, first)
    with _later(COMMAND_GRACE + 1):
        await coordinator.async_refresh()
    assert coordinator.devices[CODE].timer_choice == (first["timerSimpleTime"], 1.0)
    await hass.async_block_till_done()
    assert _select(hass).state == "1h"
    assert _end(hass).state == "Off at 11:00"
    # Replaced in the app: another end, so the HA duration no longer applies.
    _with_status(mock_api, _timer_status(4))
    with _later(COMMAND_GRACE + 2):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "running"


async def test_answer_end_differs_slightly_from_report(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Seen live: the answer's end is the cloud's, the report's the controller's (~3 s later).

    The 10 h timer set from HA must keep showing 10 h, not "running".
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
    assert end.state == "Off at 20:00"
    assert end.attributes["end_time"] == "2026-10-01T20:00:02+08:00"
    # A timer replaced elsewhere with another duration shows "running".
    _with_status(mock_api, _timer_status(9.5))
    with _later(COMMAND_GRACE + 2):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _select(hass).state == "running"


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


# ------------------------------------------------------------ clock changes


@pytest.mark.parametrize(
    ("language", "before", "after"),
    [
        ("zh-Hans", "明天 08:00 开机", "08:00 开机"),
        ("en", "On at 08:00 tomorrow", "On at 08:00"),
    ],
)
async def test_tomorrow_becomes_today_without_polling(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    freezer: FrozenDateTimeFactory,
    language: str,
    before: str,
    after: str,
) -> None:
    """Polling off: the entity stays available, and at midnight 明天 still turns into today."""
    freezer.move_to(_local(2026, 10, 1, 23, 30))
    _with_status(mock_api, _status(if_run="0", **_timer(_local(2026, 10, 2, 8, 0), turns_on=True)))
    await _setup(hass, config_entry, language=language, polling=False)
    polls = mock_api.get_status.await_count
    assert _end(hass).state == before
    await _at(hass, freezer, _local(2026, 10, 1, 23, 59, 59))
    assert _end(hass).state == before
    await _at(hass, freezer, _local(2026, 10, 2, 0, 0))
    end = _end(hass)
    assert end.state == after
    assert end.attributes["end_time"] == "2026-10-02T08:00:00+08:00"
    assert end.attributes["action"] == "on"
    # Written at the clock change, not by a poll.
    assert mock_api.get_status.await_count == polls
    # Next: when it counts as fired (sensor and select).
    fired = _local(2026, 10, 2, 8, 0) + FIRED
    assert _clock_writes(hass) == {
        _entity(hass, "sensor", "timer_end"): fired,
        _entity(hass, SELECT_DOMAIN, "simple_timer"): fired,
    }


@pytest.mark.parametrize(
    ("language", "texts"),
    [
        ("zh-Hans", ("23:55 关机", "昨天 23:55 关机", "无定时")),
        ("en", ("Off at 23:55", "Off at 23:55 yesterday", "No timer")),
    ],
)
async def test_timer_fires_without_polling(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    freezer: FrozenDateTimeFactory,
    language: str,
    texts: tuple[str, str, str],
) -> None:
    """Today, yesterday after midnight, then no timer once it counts as fired."""
    freezer.move_to(_local(2026, 10, 1, 23, 50))
    end = _local(2026, 10, 1, 23, 55)
    _with_status(mock_api, _status(**_timer(end)))
    await _setup(hass, config_entry, language=language, polling=False)
    polls = mock_api.get_status.await_count
    assert _end(hass).state == texts[0]
    assert _select(hass).state == "running"
    await _at(hass, freezer, _local(2026, 10, 2, 0, 0))
    assert _end(hass).state == texts[1]
    assert _end(hass).attributes["end_time"] == "2026-10-01T23:55:00+08:00"
    assert _select(hass).state == "running"
    await _at(hass, freezer, end + FIRED - timedelta(seconds=1))
    assert _end(hass).state == texts[1]
    await _at(hass, freezer, end + FIRED)
    state = _end(hass)
    assert state.state == texts[2]
    assert "action" not in state.attributes and "end_time" not in state.attributes
    select = _select(hass)
    assert select.state == "off"
    assert select.attributes[ATTR_OPTIONS] == ["off", *TIMER_OPTIONS]
    assert "action" not in select.attributes
    assert select.attributes["next_action"] == "off"
    assert mock_api.get_status.await_count == polls
    # Nothing changes by itself any more.
    assert _clock_writes(hass) == {}


async def test_no_clock_write_for_a_moment_already_past(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A moment not after now is never scheduled: it would come round again at once."""
    _with_status(mock_api, _timer_status(3))
    with patch("custom_components.york_smart_home.select.timer_fired_at", return_value=NOW):
        await _setup(hass, config_entry)
        writes = _clock_writes(hass)
    assert _entity(hass, SELECT_DOMAIN, "simple_timer") not in writes
    assert writes[_entity(hass, "sensor", "timer_end")] == NOW + timedelta(hours=3) + FIRED


async def test_clock_writes_follow_the_status(
    hass: HomeAssistant, mock_api, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    """Each coordinator update looks the next change up again; removal cancels it."""
    end = NOW + timedelta(minutes=30)
    _with_status(mock_api, _status(**_timer(end)))
    await _setup(hass, config_entry, polling=False)
    sensor = _entity(hass, "sensor", "timer_end")
    select = _entity(hass, SELECT_DOMAIN, "simple_timer")
    assert _clock_writes(hass) == {sensor: end + FIRED, select: end + FIRED}
    # Replaced in the app; a manual update (update_entity) reads the new end.
    later = NOW + timedelta(hours=3)
    _with_status(mock_api, _status(**_timer(later)))
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _end(hass).state == "Off at 13:00"
    assert _clock_writes(hass) == {sensor: later + FIRED, select: later + FIRED}
    # The old moment passes without a change.
    await _at(hass, freezer, end + FIRED)
    assert _end(hass).state == "Off at 13:00"
    assert _select(hass).state == "running"
    # Cancelled with the entities.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _clock_writes(hass) == {}


async def test_timer_entities_while_polls_fail(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Session left to the app: each tolerated failed poll still writes, then unavailable.

    So no stale text is shown while nothing is polled.
    """
    _with_status(mock_api, _timer_status(3))
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    updates = []
    unsub = coordinator.async_add_listener(lambda: updates.append(None))
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    for _ in range(FAILURES_BEFORE_UNAVAILABLE - 1):
        await coordinator.async_refresh()
    assert coordinator.session_yielded
    # The entities' listeners ran on every failed poll, as on a good one.
    assert len(updates) == FAILURES_BEFORE_UNAVAILABLE - 1
    assert _end(hass).state == "Off at 13:00"
    await coordinator.async_refresh()
    assert _end(hass).state == STATE_UNAVAILABLE
    assert _select(hass).state == STATE_UNAVAILABLE
    unsub()


@pytest.mark.parametrize(
    ("language", "texts"),
    [
        ("zh-Hans", ("明天 00:00 关机", "00:00 关机", "无定时")),
        ("en", ("Off at 00:00 tomorrow", "Off at 00:00", "No timer")),
    ],
)
async def test_end_at_midnight_without_polling(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    freezer: FrozenDateTimeFactory,
    language: str,
    texts: tuple[str, str, str],
) -> None:
    """An end exactly at midnight: tomorrow up to it, today from it on, then no timer."""
    midnight = _local(2026, 10, 2, 0, 0)
    freezer.move_to(_local(2026, 10, 1, 23, 30))
    _with_status(mock_api, _status(**_timer(midnight)))
    await _setup(hass, config_entry, language=language, polling=False)
    polls = mock_api.get_status.await_count
    sensor = _entity(hass, "sensor", "timer_end")
    select = _entity(hass, SELECT_DOMAIN, "simple_timer")
    assert _end(hass).state == texts[0]
    assert _clock_writes(hass) == {sensor: midnight, select: midnight + FIRED}
    await _at(hass, freezer, midnight - timedelta(microseconds=1))
    assert _end(hass).state == texts[0]
    await _at(hass, freezer, midnight)
    end = _end(hass)
    assert end.state == texts[1]
    assert end.attributes["end_time"] == "2026-10-02T00:00:00+08:00"
    assert _select(hass).state == "running"
    assert _clock_writes(hass) == {sensor: midnight + FIRED, select: midnight + FIRED}
    await _at(hass, freezer, midnight + FIRED - timedelta(milliseconds=1))
    assert _end(hass).state == texts[1]
    await _at(hass, freezer, midnight + FIRED)
    assert _end(hass).state == texts[2]
    assert _select(hass).state == "off"
    assert mock_api.get_status.await_count == polls
    assert _clock_writes(hass) == {}


@pytest.mark.parametrize(
    ("language", "before", "after"),
    [
        ("zh-Hans", "已设定定时", "明天 12:00 关机"),
        ("en", "Timer set", "Off at 12:00 tomorrow"),
    ],
)
async def test_end_comes_within_25_hours_without_polling(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    freezer: FrozenDateTimeFactory,
    language: str,
    before: str,
    after: str,
) -> None:
    """An end over 25 h ahead is not shown ("Timer set") until it is within 25 h."""
    end = NOW + timedelta(hours=26)  # 12:00 tomorrow
    _with_status(mock_api, _status(**_timer(end)))
    await _setup(hass, config_entry, language=language, polling=False)
    polls = mock_api.get_status.await_count
    sensor = _entity(hass, "sensor", "timer_end")
    state = _end(hass)
    assert state.state == before
    assert state.attributes["action"] == "off"
    assert "end_time" not in state.attributes
    assert _clock_writes(hass)[sensor] == NOW + timedelta(hours=1)
    await _at(hass, freezer, NOW + timedelta(hours=1) - timedelta(milliseconds=1))
    assert _end(hass).state == before
    await _at(hass, freezer, NOW + timedelta(hours=1))
    state = _end(hass)
    assert state.state == after
    assert state.attributes["end_time"] == "2026-10-02T12:00:00+08:00"
    assert state.attributes["action"] == "off"
    assert _select(hass).state == "running"
    assert mock_api.get_status.await_count == polls
    # Next: midnight, when tomorrow becomes today.
    assert _clock_writes(hass)[sensor] == _local(2026, 10, 2, 0, 0)


def _entity_object(hass: HomeAssistant, entity_id: str):
    """The entity object behind an entity id."""
    return next(
        platform.entities[entity_id]
        for platform in async_get_platforms(hass, DOMAIN)
        if entity_id in platform.entities
    )


async def test_one_moment_per_state_write(
    hass: HomeAssistant, mock_api, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    """A state write reads the clock once, even if the timer fires during the write.

    Here each reading of the clock is a millisecond after the one before,
    the first just before the timer counts as fired. The state, the listed
    options and the attributes are all those of the update's moment: the
    select never reads "unknown" (HA takes its state from the option and
    checks it against the options), and the text agrees with its attributes.
    """
    end = NOW + timedelta(minutes=30)
    _with_status(mock_api, _status(**_timer(end)))
    await _setup(hass, config_entry, polling=False)
    select_id = _entity(hass, SELECT_DOMAIN, "simple_timer")
    sensor_id = _entity(hass, "sensor", "timer_end")
    edge = end + timedelta(milliseconds=TIMER_FIRED_MS)  # fired right after it
    for entity_id in (select_id, sensor_id):
        ticks = itertools.count()

        def clock(time_zone=None, ticks=ticks):
            moment = edge + timedelta(milliseconds=next(ticks) - 0.5)
            return moment.astimezone(time_zone or SHANGHAI)

        with (
            patch.object(dt_util, "now", side_effect=clock),
            patch.object(dt_util, "utcnow", side_effect=lambda clock=clock: clock(UTC)),
        ):
            _entity_object(hass, entity_id)._handle_coordinator_update()
    select = _select(hass)
    assert select.state == "running"
    assert select.attributes[ATTR_OPTIONS] == ["off", "running", *TIMER_OPTIONS]
    assert select.attributes["action"] == "off"
    state = _end(hass)
    assert state.state == "Off at 10:30"
    assert state.attributes["end_time"] == "2026-10-01T10:30:00+08:00"
    assert state.attributes["action"] == "off"
    # Written again once the timer counts as fired.
    assert _clock_writes(hass) == {select_id: end + FIRED, sensor_id: end + FIRED}
    await _at(hass, freezer, end + FIRED)
    assert _select(hass).state == "off"
    assert _end(hass).state == "No timer"


async def test_time_zone_and_language_changes(
    hass: HomeAssistant, mock_api, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    """A new time zone or language of Home Assistant applies at once, also without polling."""
    freezer.move_to(_local(2026, 10, 1, 22, 0))
    _with_status(mock_api, _status(if_run="0", **_timer(_local(2026, 10, 2, 8, 0), turns_on=True)))
    await _setup(hass, config_entry, polling=False)
    polls = mock_api.get_status.await_count
    sensor = _entity(hass, "sensor", "timer_end")
    assert _end(hass).state == "On at 08:00 tomorrow"
    assert _clock_writes(hass)[sensor] == _local(2026, 10, 2, 0, 0)
    # 22:00 in Shanghai is 15:00 in London (summer time); the end is 01:00 there.
    london = ZoneInfo("Europe/London")
    await hass.config.async_update(time_zone="Europe/London")
    await hass.async_block_till_done()
    end = _end(hass)
    assert end.state == "On at 01:00 tomorrow"
    assert end.attributes["end_time"] == "2026-10-02T01:00:00+01:00"
    assert _clock_writes(hass)[sensor] == datetime(2026, 10, 2, 0, 0, tzinfo=london)
    await hass.config.async_update(language="zh-Hans")
    await hass.async_block_till_done()
    assert _end(hass).state == "明天 01:00 开机"
    # Midnight in London: tomorrow becomes today.
    await _at(hass, freezer, datetime(2026, 10, 2, 0, 0, tzinfo=london))
    assert _end(hass).state == "01:00 开机"
    assert mock_api.get_status.await_count == polls


async def test_no_timer_text_follows_the_language(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Without a timer nothing is planned; the text still follows a new language at once."""
    await _setup(hass, config_entry, polling=False)
    assert _end(hass).state == "No timer"
    assert _clock_writes(hass) == {}
    await hass.config.async_update(language="zh-Hans")
    await hass.async_block_till_done()
    assert _end(hass).state == "无定时"
    await hass.config.async_update(language="en")
    await hass.async_block_till_done()
    assert _end(hass).state == "No timer"
    # The two clock entities (sensor and select) stop listening when removed.
    listeners = hass.bus.async_listeners()[EVENT_CORE_CONFIG_UPDATE]
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.bus.async_listeners().get(EVENT_CORE_CONFIG_UPDATE, 0) == listeners - 2


async def test_clock_entity_renamed(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A new entity id re-adds the entity: its clock write and listener move, none is left."""
    end = NOW + timedelta(minutes=30)
    _with_status(mock_api, _status(**_timer(end)))
    await _setup(hass, config_entry, polling=False)
    select = _entity(hass, SELECT_DOMAIN, "simple_timer")
    listeners = hass.bus.async_listeners()[EVENT_CORE_CONFIG_UPDATE]
    renamed = "sensor.york_timer_end_renamed"
    er.async_get(hass).async_update_entity(
        _entity(hass, "sensor", "timer_end"), new_entity_id=renamed
    )
    await hass.async_block_till_done()
    assert _entity(hass, "sensor", "timer_end") == renamed
    assert hass.states.get(renamed).state == "Off at 10:30"
    assert _clock_writes(hass) == {renamed: end + FIRED, select: end + FIRED}
    assert hass.bus.async_listeners()[EVENT_CORE_CONFIG_UPDATE] == listeners
    await hass.config.async_update(language="zh-Hans")
    await hass.async_block_till_done()
    assert hass.states.get(renamed).state == "10:30 关机"
