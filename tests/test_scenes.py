"""Cloud scenes (智能场景): parsing, entities, requests, session handling, cadence."""

from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import timedelta
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.york_smart_home.api import (
    Session,
    YorkApiError,
    YorkClientOutdatedError,
    YorkCommandError,
    YorkConnectionError,
    YorkRateLimitError,
    YorkSceneConflictError,
    YorkSessionTakenError,
)
from custom_components.york_smart_home.const import (
    API_BASE,
    DOMAIN,
    SCENE_INTERVAL,
    SCENE_RESULT_POLLS,
)
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.scenes import (
    AUTO,
    OPEN_FLAG,
    RECOMMENDED,
    REPLAY,
    RUN,
    SCENE_TYPES,
    SWITCH,
    has_devices,
    module_succeeded,
    replay_request,
    result_outcome,
    scene_device_count,
    scene_is_on,
    scene_name,
    scene_status,
    scene_target_reached,
    scene_type,
)
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import CODE, FIXTURE, SCENES
from .test_api import OK, UNAUTHORIZED, _api, _login_body, _sequence
from .test_integration import _setup
from .test_session import _issue, _later, _yield

HOME = 100001
SLEEP_ID = 300003
UPDATE_TIME = 1700000000002
BASE = Path(__file__).parent.parent / "custom_components" / DOMAIN

# A wire controller's entry in a scene: the full command the scene editor
# stores (if_run, the mode and fan keys, set_temp + set_temperature_add05).
XKQ_ENTRY = {
    "xkqCode": CODE,
    "deviceType": "C",
    "xkqType": "9",
    "ctrlJson": {
        "if_run": "1",
        "wire_controller_makecold": "1",
        "wire_controller_wind_low": "1",
        "set_temp": 26,
        "set_temperature_add05": "0",
        "set_humidity": 50,
    },
    "createUser": 200002,
    "updateTime": 1700000000050,
}


def _scene(smart_id: int, scene_type_code: str, *, devices: bool = True, **fields) -> dict:
    """A scene record shaped like the account's (fake ids)."""
    record = copy.deepcopy(SCENES[0])
    record.update(
        smartId=smart_id,
        type=scene_type_code,
        smartName=f"场景{scene_type_code}",
        updateTime=1700000000000 + smart_id,
        xkqList=[copy.deepcopy(XKQ_ENTRY)] if devices else [],
    )
    record.update(fields)
    return record


def _serve(mock_api, records: list[dict]) -> None:
    mock_api.get_scenes.side_effect = lambda home: copy.deepcopy(records)


def _entity(hass: HomeAssistant, platform: str, smart_id: int, home: int = HOME) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"home_{home}_scene_{smart_id}")


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def _switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )


@pytest.fixture
def fast_results():
    """Check scene results without the app's 2 s wait."""
    with patch("custom_components.york_smart_home.coordinator.SCENE_RESULT_INTERVAL", 0):
        yield


@contextmanager
def _no_refresh(entry):
    """Hold the refresh after an action, to see the state shown right away."""
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        yield
    entry.runtime_data._cancel_control_refresh()


async def _after_action(hass: HomeAssistant) -> None:
    """Fire the refresh an action schedules (REFRESH_DELAY_AFTER_CONTROL)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()


def _warnings(caplog) -> list[str]:
    """Warnings of this integration (not Home Assistant's own)."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith("custom_components.york_smart_home")
    ]


# ===================================================================== rules


def test_scene_types_like_the_app() -> None:
    """The scenes tab's control per type (template chunk 18, ScenesPage chunk 15)."""
    runs = {code for code, stype in SCENE_TYPES.items() if stype.control == RUN}
    switches = {code for code, stype in SCENE_TYPES.items() if stype.control == SWITCH}
    assert runs == {"1", "2", "8", "F", "G", "I"}
    assert switches == {"5", "6", "7", "9", "B", "C", "D", "E", "H", "J", "K", "L", "M", "O"}
    # Replayed through multiIuOuCtrl, with the home page's ctrlTypes.
    replays = {code: s.ctrl_type for code, s in SCENE_TYPES.items() if s.request == REPLAY}
    assert replays == {"1": "YO431", "2": "YO432", "8": "YO438", "F": "YO43F"}
    by_request = {
        request: {code for code, s in SCENE_TYPES.items() if s.request == request}
        for request in (OPEN_FLAG, RECOMMENDED, AUTO)
    }
    assert by_request == {
        OPEN_FLAG: {"5", "6", "7", "9", "M", "O"},
        RECOMMENDED: {"G", "I", "J", "K", "L"},
        AUTO: {"B", "C", "D", "E", "H"},
    }
    # Never listed by the app: no entity.
    for code in ("3", "4", "A", "N", "Z", "X", "", None):
        assert scene_type({"type": code}) is None
    assert scene_type({}) is None
    # The type is compared as text (8 and "8" alike).
    assert scene_type({"type": 8}) is SCENE_TYPES["8"]


def test_scene_state() -> None:
    sleep = SCENE_TYPES["5"]
    for value, on, status in (
        ("1", True, "on"),
        (1, True, "on"),
        ("0", False, "off"),
        (0, False, "off"),
        # HANGUP: every openFlg toggle of the app shows it as off.
        ("2", False, "hangup"),
        (None, False, None),
        ("x", False, None),
        (True, False, None),
    ):
        record = {"type": "5", "openFlg": value}
        assert scene_is_on(record, sleep) is on, value
        assert scene_status(record, sleep) == status, value
    # Linkage and pets scenes keep their state in actionFlg; HANGUP is running.
    pets = SCENE_TYPES["H"]
    assert scene_is_on({"actionFlg": "1", "openFlg": "0"}, pets)
    assert scene_is_on({"actionFlg": "2"}, pets)
    assert scene_status({"actionFlg": "2"}, pets) == "hangup"
    assert not scene_is_on({"actionFlg": "0", "openFlg": "1"}, pets)
    assert not scene_is_on({"actionFlg": None}, SCENE_TYPES["B"])


def test_target_reached() -> None:
    sleep, summer = SCENE_TYPES["5"], SCENE_TYPES["B"]
    assert scene_target_reached({"openFlg": "1"}, sleep, True)
    assert scene_target_reached({"openFlg": "0"}, sleep, False)
    assert not scene_target_reached({"openFlg": "0"}, sleep, True)
    # A hung-up scene is sent the explicit state either way...
    assert not scene_target_reached({"openFlg": "2"}, sleep, True)
    assert not scene_target_reached({"openFlg": "2"}, sleep, False)
    assert not scene_target_reached({}, sleep, False)
    # ...but a hung-up linkage scene already runs.
    assert scene_target_reached({"actionFlg": "2"}, summer, True)
    assert not scene_target_reached({"actionFlg": "2"}, summer, False)


def test_devices() -> None:
    assert not has_devices(SCENES[0])  # the account's sleep scene
    assert not has_devices({"xkqList": None, "iuList": None})
    assert not has_devices({"xkqList": {"a": 1}})
    assert has_devices({"xkqList": [XKQ_ENTRY], "iuList": []})
    assert has_devices({"iuList": [{"iuId": "x"}]})
    # Like the app's length checks, any entry counts.
    assert scene_device_count({"xkqList": [1, 2], "iuList": [3]}) == 3


def test_scene_name() -> None:
    assert scene_name(SCENES[0]) == "睡眠"
    assert scene_name({"type": "5", "smartName": "  "}) == "睡眠曲线"
    assert scene_name({"type": "8", "smartName": None}) == "手动场景"
    assert scene_name({"type": "Z", "smartId": 300099}) == "300099"


def test_replay_wire_controllers() -> None:
    """xkqCtrlList is the scene's xkqList, entries copied as stored."""
    record = _scene(300004, "8")
    record["xkqList"].append({"xkqCode": "0000000000000002", "xkqType": "8", "ctrlJson": "{}"})
    request = replay_request(record, SCENE_TYPES["8"])
    assert request == {
        "ctrl_type": "YO438",
        "xkq_ctrl_list": [
            {
                "xkqCode": CODE,
                "deviceType": "C",
                "xkqType": "9",
                "ctrlJson": XKQ_ENTRY["ctrlJson"],
            },
            # A field the record lacks is left out (JSON drops undefined).
            {"xkqCode": "0000000000000002", "xkqType": "8", "ctrlJson": "{}"},
        ],
        "ctrl_list": [],
        "scene_id": None,
    }
    for code, ctrl_type in (("1", "YO431"), ("2", "YO432")):
        assert replay_request(_scene(300005, code), SCENE_TYPES[code])["ctrl_type"] == ctrl_type
    fast_heat = replay_request(_scene(300006, "F", devices=False), SCENE_TYPES["F"])
    assert fast_heat["ctrl_type"] == "YO43F"
    assert fast_heat["scene_id"] == 300006
    assert fast_heat["xkq_ctrl_list"] == []


def test_replay_gateway_units() -> None:
    """ctrlList as the scenes tab builds it (runManualScenes)."""
    unit = {"systemNo": "00", "iuSn": "S1", "iuNo": "01", "iuType": "11", "iuId": "u1"}
    command = {"onoff": "1", "mode": 1, "temp": 26, "wind": 3, "humi": 50, "extra": 1}
    record = _scene(
        300007,
        "8",
        devices=False,
        iuList=[
            {**unit, "iezCode": "G1", "deviceType": "1", "ctrlJson": command},
            {**unit, "iezCode": "G2", "deviceType": "E", "iuNo": "02", "ctrlJson": command},
            {**unit, "iezCode": "G1", "deviceType": "1", "iuNo": "03"},
            {**unit, "iezCode": "G3", "deviceType": "6"},
            {**unit, "deviceType": "1", "ctrlJson": command},  # no gateway: dropped
            "not a unit",
        ],
    )
    gateway_unit = {"systemNo": "00", "iuSn": "S1", "iuNo": "01", "iuType": "11"}
    assert replay_request(record, SCENE_TYPES["8"])["ctrl_list"] == [
        {
            "iuCtrlInfo": [
                # Other gateways keep only onoff/mode/temp/wind/humi.
                {**gateway_unit, "ctrlJson": {k: v for k, v in command.items() if k != "extra"}},
                {**gateway_unit, "iuNo": "03", "ctrlJson": {}},
            ],
            "ouCtrlInfo": [],
            "iezCode": "G1",
            "deviceType": "1",
        },
        {
            # cat ("E") and ZHP ("6") units keep the stored ctrlJson.
            "iuCtrlInfo": [{**gateway_unit, "iuNo": "02", "ctrlJson": command}],
            "ouCtrlInfo": [],
            "iezCode": "G2",
            "deviceType": "E",
        },
        {"iuCtrlInfo": [gateway_unit], "ouCtrlInfo": [], "iezCode": "G3", "deviceType": "6"},
    ]


def test_result_codes() -> None:
    """getControlResults: 200 done; 201/206/207/208/212/220 failed; others pending."""
    assert result_outcome("200") is True
    for code in ("201", "206", "207", "208", "212", "220"):
        assert result_outcome(code) is False
    for code in ("205", "400", "500", "999"):
        assert result_outcome(code) is None
    assert module_succeeded([{"moduleResult": "1"}, {"moduleResult": "2"}])
    assert module_succeeded([{"moduleResult": 2}])
    assert not module_succeeded([{"moduleResult": "1"}, "x"])
    assert not module_succeeded(None)


def test_scene_translations() -> None:
    for path in (BASE / "strings.json", BASE / "translations" / "zh-Hans.json"):
        strings = json.loads(path.read_text("utf-8"))
        assert "{home}" in strings["device"]["home"]["name"]
        for platform in ("button", "switch"):
            attributes = strings["entity"][platform]["scene"]["state_attributes"]
            states = attributes["scene_type"]["state"]
            assert set(states) == {stype.key for stype in SCENE_TYPES.values()}
        status = strings["entity"]["switch"]["scene"]["state_attributes"]["status"]["state"]
        assert set(status) == {"on", "off", "hangup"}
        for key in ("scene_gone", "scene_no_devices", "scene_conflict", "scene_rejected"):
            assert key in strings["exceptions"]
        assert "{scene}" in strings["exceptions"]["scene_no_devices"]["message"]
        assert "{message}" in strings["exceptions"]["scene_conflict"]["message"]


# ======================================================================= api

SMARTS_URL = f"{API_BASE}/api/apptimer/homes/{HOME}/smarts"
CTRL_URL = f"{API_BASE}/api/appcontrol/cmds/multiIuOuCtrl"
RESULT_URL = f"{API_BASE}/api/appcontrol/cmds/multiCtrlResult"


async def test_get_scenes(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    sleep = copy.deepcopy(SCENES[0])
    aioclient_mock.get(
        SMARTS_URL, json={**OK, "data": {"smartList": [sleep, {"smartName": "x"}, "bad"]}}
    )
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.get_scenes(HOME) == [sleep]  # records without smartId skipped
    (call,) = aioclient_mock.mock_calls
    assert call[0] == "GET"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({**OK, "data": {"smartList": []}}, []),
        ({**OK, "data": {"smartList": None}}, []),
        ({**OK, "data": {}}, []),
        ({**OK, "data": None}, YorkApiError),
        ({**OK, "data": {"smartList": {"a": 1}}}, YorkApiError),
        ({"code": "500", "message": "系统异常"}, YorkApiError),
    ],
)
async def test_get_scenes_answers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, answer, expected
) -> None:
    aioclient_mock.get(SMARTS_URL, json=answer)
    api = _api(hass, auth=Session("tok", 200002))
    if isinstance(expected, list):
        assert await api.get_scenes(HOME) == expected
    else:
        with pytest.raises(expected):
            await api.get_scenes(HOME)


async def test_run_scene_body(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The app's ZL0E body: position defaults, both lists, homeId, ctrlType."""
    tokens = [{"iezCode": CODE, "hiiotToken": "h1", "hiiotErrorCode": 200}]
    aioclient_mock.post(CTRL_URL, json={**OK, "data": tokens})
    api = _api(hass, auth=Session("tok", 200002))
    xkq = [{k: XKQ_ENTRY[k] for k in ("xkqCode", "deviceType", "xkqType", "ctrlJson")}]
    gateways = [{"iuCtrlInfo": [], "ouCtrlInfo": [], "iezCode": "G1", "deviceType": "1"}]
    assert await api.run_scene(HOME, "YO438", xkq, gateways) == tokens
    body = aioclient_mock.mock_calls[0][2]
    assert body == {
        "regionCode": "000000",
        "positionLng": 0,
        "positionLat": 0,
        "position1": "",
        "position2": "",
        "positionCity": "",
        "ctrlList": gateways,
        "xkqCtrlList": xkq,
        "homeId": HOME,
        "ctrlType": "YO438",
    }
    assert "sceneId" not in body
    await api.run_scene(HOME, "YO43F", [], [], scene_id=300006)
    assert aioclient_mock.mock_calls[1][2]["sceneId"] == 300006
    assert aioclient_mock.mock_calls[1][2]["ctrlType"] == "YO43F"


async def test_run_scene_answers(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        CTRL_URL,
        side_effect=_sequence(
            {"code": "500", "message": "设备不在线"},
            OK,  # no data: nothing to follow
            UNAUTHORIZED,
            {**OK, "data": [{"iezCode": CODE}]},
        ),
    )
    aioclient_mock.get(f"{API_BASE}/login", json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("old", 200002))
    with pytest.raises(YorkCommandError, match="设备不在线"):
        await api.run_scene(HOME, "YO438", [], [])
    assert await api.run_scene(HOME, "YO438", [], []) == []
    # A user action: a session taken by the app is taken back.
    assert await api.run_scene(HOME, "YO438", [], []) == [{"iezCode": CODE}]
    assert api.auth.token == "fresh"


async def test_control_results_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    tokens = [{"iezCode": CODE, "hiiotToken": "h1"}]
    aioclient_mock.post(
        RESULT_URL,
        side_effect=_sequence({"code": "205", "message": "执行中", "data": []}, UNAUTHORIZED),
    )
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    answer = await api.get_control_results(tokens)
    assert answer["code"] == "205"  # the envelope, whatever the code
    assert aioclient_mock.mock_calls[0][2] == {"tokenList": tokens}
    # A background check never takes the session back.
    with pytest.raises(YorkSessionTakenError):
        await api.get_control_results(tokens)


async def test_open_flag_request(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    url = f"{API_BASE}/api/apptimer/homes/{HOME}/smarts/{SLEEP_ID}/openFlg"
    aioclient_mock.put(
        url,
        side_effect=_sequence(
            {**OK, "data": {"updateTime": UPDATE_TIME + 1}},
            {"code": "500", "message": "数据已被修改"},
            {"code": "223", "message": "与其他场景冲突"},
            OK,
        ),
    )
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME) == {
        "updateTime": UPDATE_TIME + 1
    }
    method, _, body, _ = aioclient_mock.mock_calls[0]
    assert method == "PUT"
    # The scenes tab's body: the new flag and the latest updateTime (a number).
    assert body == {"openFlg": "1", "updateTime": UPDATE_TIME}
    with pytest.raises(YorkCommandError, match="数据已被修改") as info:
        await api.set_scene_open(HOME, SLEEP_ID, "0", UPDATE_TIME)
    assert not isinstance(info.value, YorkSceneConflictError)
    with pytest.raises(YorkSceneConflictError, match="与其他场景冲突"):
        await api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME)
    assert await api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME) == {}


async def test_recommended_scene_requests(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    vacation = f"{API_BASE}/api/apptimer/homes/{HOME}/scene/type/K/300011"
    party = f"{API_BASE}/api/apptimer/homes/{HOME}/scene/type/I/300012"
    aioclient_mock.post(
        vacation,
        side_effect=_sequence(
            {**OK, "data": {"updateTime": 5}}, {"code": "223", "message": "与度假场景冲突"}
        ),
    )
    aioclient_mock.post(party, json={**OK, "data": {"upadteTime": 6}})
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.run_recommended_scene(HOME, "K", 300011, 4, open_flg="1") == {"updateTime": 5}
    assert aioclient_mock.mock_calls[0][2] == {
        "homeId": HOME,
        "sceneId": 300011,
        "openFlg": "1",
        "type": "K",
        "updateTime": 4,
        "forceOpenFlg": "0",  # never "1": that would switch the other scene off
    }
    with pytest.raises(YorkSceneConflictError, match="与度假场景冲突"):
        await api.run_recommended_scene(HOME, "K", 300011, 5, open_flg="1")
    # Running a party scene once: no openFlg, no forceOpenFlg.
    assert await api.run_recommended_scene(HOME, "I", 300012, 7) == {"upadteTime": 6}
    assert aioclient_mock.mock_calls[2][2] == {
        "homeId": HOME,
        "sceneId": 300012,
        "type": "I",
        "updateTime": 7,
    }


async def test_auto_scene_request(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    url = f"{API_BASE}/api/apptimer/homes/{HOME}/newScenes/300013/flag/0"
    aioclient_mock.put(
        url,
        side_effect=_sequence(
            {**OK, "data": {"updateTime": 9}},
            {**OK, "data": {"hcScenes": [{"smartName": "冬季", "smartId": 1}, {"x": 1}]}},
            {**OK, "data": {}},
            {"code": "500", "message": "失败"},
        ),
    )
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.set_auto_scene(HOME, 300013, "B", "1", 8) == {"updateTime": 9}
    method, called, body, _ = aioclient_mock.mock_calls[0]
    assert method == "PUT"
    assert str(called).endswith("/newScenes/300013/flag/0")
    assert body == {
        "hcScenes": [],
        "updateTime": 8,
        "type": "B",
        "actionFlg": "1",
        "flag": "0",  # never "1": that would switch the conflicting scenes off
        "homeId": HOME,
        "smartId": 300013,
    }
    # No updateTime: the scene conflicts with the listed ones.
    with pytest.raises(YorkSceneConflictError, match="^场景冬季$"):
        await api.set_auto_scene(HOME, 300013, "B", "1", 8)
    with pytest.raises(YorkSceneConflictError, match="conflict"):
        await api.set_auto_scene(HOME, 300013, "B", "1", 8)
    with pytest.raises(YorkCommandError, match="失败"):
        await api.set_auto_scene(HOME, 300013, "B", "0", 8)


async def test_scene_change_reclaims_session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    url = f"{API_BASE}/api/apptimer/homes/{HOME}/smarts/{SLEEP_ID}/openFlg"
    aioclient_mock.put(url, side_effect=_sequence(UNAUTHORIZED, OK))
    aioclient_mock.get(f"{API_BASE}/login", json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("old", 200002))
    await api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME)
    assert api.auth.token == "fresh"


# ================================================================== entities


async def test_account_sleep_scene(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The account's only scene: the app shows a toggle, off, for a sleep curve."""
    await _setup(hass, config_entry)
    mock_api.get_scenes.assert_awaited_once_with(HOME)
    entity_id = _entity(hass, SWITCH_DOMAIN, SLEEP_ID)
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["friendly_name"].endswith("睡眠")
    assert state.attributes["scene_type"] == "sleep_curve"
    assert state.attributes["status"] == "off"
    assert state.attributes["devices"] == 0
    assert _entity(hass, BUTTON_DOMAIN, SLEEP_ID) is None

    # A device for the home, separate from the wire controller's.
    entry = er.async_get(hass).async_get(entity_id)
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device.identifiers == {(DOMAIN, f"home_{HOME}")}
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.name == f"{FIXTURE['device']['homeName']} York Smart Home"
    climate = er.async_get(hass).async_get(
        er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)
    )
    assert climate.device_id != device.id


async def test_home_device_name_translated(hass: HomeAssistant, mock_api, config_entry) -> None:
    hass.config.language = "zh-Hans"
    await _setup(hass, config_entry)
    entry = er.async_get(hass).async_get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID))
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device.name == f"{FIXTURE['device']['homeName']} 约克智慧家"


async def test_controller_entities_unchanged(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The scene adds its own entity; the controller's entities stay the same."""
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    with_scene = {e.unique_id for e in registry.entities.values() if e.platform == DOMAIN}
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    for entity_id in [e.entity_id for e in registry.entities.values()]:
        registry.async_remove(entity_id)
    mock_api.get_scenes.side_effect = lambda home: []
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    without = {e.unique_id for e in registry.entities.values() if e.platform == DOMAIN}
    assert with_scene - without == {f"home_{HOME}_scene_{SLEEP_ID}"}
    assert without < with_scene
    assert all(uid == CODE or uid.startswith(f"{CODE}_") for uid in without)


async def test_entities_per_type(hass: HomeAssistant, mock_api, config_entry) -> None:
    codes = "123456789ABCDEFGHIJKLMNOZ"
    records = [_scene(300100 + i, code) for i, code in enumerate(codes)]
    _serve(mock_api, records)
    await _setup(hass, config_entry)
    made = {}
    for i, code in enumerate(codes):
        button = _entity(hass, BUTTON_DOMAIN, 300100 + i)
        switch = _entity(hass, SWITCH_DOMAIN, 300100 + i)
        assert not (button and switch)
        made[code] = "button" if button else "switch" if switch else None
    assert {code for code, kind in made.items() if kind == "button"} == set("128FGI")
    assert {code for code, kind in made.items() if kind == "switch"} == set("5679BCDEHJKLMO")
    assert {code for code, kind in made.items() if kind is None} == set("34ANZ")
    button = hass.states.get(_entity(hass, BUTTON_DOMAIN, 300107))  # "8"
    assert button.attributes["scene_type"] == "manual"
    assert button.attributes["devices"] == 1
    assert "status" not in button.attributes


async def test_sleep_scene_without_devices(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Like the app: switching on a scene without devices is refused, nothing is sent."""
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, SLEEP_ID)
    scenes_read = mock_api.get_scenes.await_count
    with pytest.raises(ServiceValidationError) as info:
        await _switch(hass, entity_id, True)
    assert info.value.translation_key == "scene_no_devices"
    assert info.value.translation_placeholders == {"scene": "睡眠"}
    await _switch(hass, entity_id, False)  # already off
    await hass.async_block_till_done()
    mock_api.set_scene_open.assert_not_called()
    mock_api.relogin.assert_not_called()
    assert mock_api.get_scenes.await_count == scenes_read
    assert hass.states.get(entity_id).state == "off"


async def test_switch_scene(hass: HomeAssistant, mock_api, config_entry) -> None:
    timer = _scene(300004, "O")
    records = [SCENES[0], timer]
    _serve(mock_api, records)
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    assert hass.states.get(entity_id).state == "off"
    reads = mock_api.get_scenes.await_count

    mock_api.set_scene_open.return_value = {"updateTime": 1700000300004}
    await _switch(hass, entity_id, True)
    mock_api.set_scene_open.assert_awaited_once_with(HOME, 300004, "1", timer["updateTime"])
    assert hass.states.get(entity_id).state == "on"  # shown right away
    # The refresh scheduled by the action reads the list again (the cloud
    # still says off here).
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 1
    assert hass.states.get(entity_id).state == "off"

    # The next change sends the token of the list just read.
    records[1] = {**timer, "openFlg": "1", "updateTime": 1700000300005}
    with _later(SCENE_INTERVAL + 1):
        await config_entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == "on"
    await _switch(hass, entity_id, True)  # already on: nothing sent
    assert mock_api.set_scene_open.await_count == 1
    await _switch(hass, entity_id, False)
    assert mock_api.set_scene_open.await_args.args == (HOME, 300004, "0", 1700000300005)


async def test_switch_keeps_answer_token(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The answer's updateTime is the token of the next change."""
    _serve(mock_api, [_scene(300004, "5")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.set_scene_open.return_value = {"updateTime": 42}
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await coordinator.async_set_scene(HOME, "300004", True)
        assert coordinator.homes[HOME].scenes["300004"].record["updateTime"] == 42
        await coordinator.async_set_scene(HOME, "300004", False)
        assert mock_api.set_scene_open.await_args.args == (HOME, 300004, "0", 42)
        # Without updateTime in the answer the old token stays until the
        # list is read again.
        mock_api.set_scene_open.return_value = {}
        await coordinator.async_set_scene(HOME, "300004", True)
        assert coordinator.homes[HOME].scenes["300004"].record["updateTime"] == 42
        assert coordinator.homes[HOME].scenes["300004"].record["openFlg"] == "1"
    coordinator._cancel_control_refresh()


async def test_switch_scene_rejected(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [_scene(300004, "O")])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    reads = mock_api.get_scenes.await_count
    mock_api.set_scene_open.side_effect = YorkCommandError("数据已被修改，请刷新")
    with pytest.raises(HomeAssistantError) as info:
        await _switch(hass, entity_id, True)
    assert info.value.translation_key == "scene_rejected"
    assert info.value.translation_placeholders == {
        "scene": "场景O",
        "message": "数据已被修改，请刷新",
    }
    assert hass.states.get(entity_id).state == "off"
    # A failed change reads the list again too (a stale token is replaced).
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 1


async def test_recommended_scene_conflict(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Vacation: the app's POST with forceOpenFlg "0"; a conflict is never forced."""
    vacation = _scene(300011, "K")
    _serve(mock_api, [vacation])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, 300011)
    mock_api.run_recommended_scene.side_effect = YorkSceneConflictError("与两联供智慧除湿冲突")
    with pytest.raises(HomeAssistantError) as info:
        await _switch(hass, entity_id, True)
    assert info.value.translation_key == "scene_conflict"
    assert info.value.translation_placeholders["message"] == "与两联供智慧除湿冲突"
    mock_api.run_recommended_scene.assert_awaited_once_with(
        HOME, "K", 300011, vacation["updateTime"], open_flg="1"
    )
    assert hass.states.get(entity_id).state == "off"

    mock_api.run_recommended_scene.side_effect = None
    with _no_refresh(config_entry):
        await _switch(hass, entity_id, True)
    assert hass.states.get(entity_id).state == "on"
    assert mock_api.run_recommended_scene.await_count == 2


async def test_auto_scenes(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Linkage and pets scenes: actionFlg, HANGUP counts as running."""
    pets = _scene(300013, "H", actionFlg="2", openFlg="0")
    summer = _scene(300014, "B", actionFlg="0")
    _serve(mock_api, [pets, summer])
    await _setup(hass, config_entry)
    pets_id = _entity(hass, SWITCH_DOMAIN, 300013)
    summer_id = _entity(hass, SWITCH_DOMAIN, 300014)
    state = hass.states.get(pets_id)
    assert state.state == "on"
    assert state.attributes["status"] == "hangup"
    with _no_refresh(config_entry):
        await _switch(hass, pets_id, True)  # already running
        mock_api.set_auto_scene.assert_not_called()
        await _switch(hass, pets_id, False)
        mock_api.set_auto_scene.assert_awaited_once_with(HOME, 300013, "H", "0", pets["updateTime"])
        assert hass.states.get(pets_id).state == "off"

        await _switch(hass, summer_id, True)
        assert mock_api.set_auto_scene.await_args.args == (
            HOME,
            300014,
            "B",
            "1",
            summer["updateTime"],
        )
        assert hass.states.get(summer_id).state == "on"
        mock_api.set_auto_scene.side_effect = YorkSceneConflictError("场景冬季防过冷场景")
        with pytest.raises(HomeAssistantError) as info:
            await _switch(hass, summer_id, False)
        assert info.value.translation_key == "scene_conflict"
        assert hass.states.get(summer_id).state == "on"


async def test_run_manual_scene(
    hass: HomeAssistant, mock_api, config_entry, fast_results, caplog
) -> None:
    manual = _scene(300004, "8")
    _serve(mock_api, [manual])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, BUTTON_DOMAIN, 300004)
    tokens = [{"iezCode": CODE, "hiiotToken": "h1", "hiiotErrorCode": 200}]
    mock_api.run_scene.return_value = tokens
    mock_api.get_control_results.side_effect = [
        {"code": "205", "message": "执行中", "data": []},
        {"code": "200", "message": "控制成功", "data": [{"moduleResult": "2"}]},
    ]
    with caplog.at_level(logging.DEBUG, logger="custom_components.york_smart_home"):
        await _press(hass, entity_id)
        mock_api.run_scene.assert_awaited_once_with(
            HOME,
            "YO438",
            [{k: XKQ_ENTRY[k] for k in ("xkqCode", "deviceType", "xkqType", "ctrlJson")}],
            [],
            scene_id=None,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    # Followed like the scenes tab: until a final code.
    assert mock_api.get_control_results.await_count == 2
    assert mock_api.get_control_results.await_args.args == (tokens,)
    assert "carried out" in caplog.text
    assert not _warnings(caplog)
    mock_api.control.assert_not_called()


@pytest.mark.parametrize(
    ("code", "ctrl_type", "scene_id"),
    [("1", "YO431", None), ("2", "YO432", None), ("F", "YO43F", 300004)],
)
async def test_run_ctrl_types(
    hass: HomeAssistant, mock_api, config_entry, fast_results, code, ctrl_type, scene_id
) -> None:
    _serve(mock_api, [_scene(300004, code)])
    await _setup(hass, config_entry)
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    args = mock_api.run_scene.await_args
    assert args.args[:2] == (HOME, ctrl_type)
    assert args.kwargs == {"scene_id": scene_id}


async def test_run_scene_with_gateway_units(
    hass: HomeAssistant, mock_api, config_entry, fast_results
) -> None:
    unit = {
        "iezCode": "G1",
        "deviceType": "6",
        "systemNo": "00",
        "iuSn": "S1",
        "iuNo": "01",
        "iuType": "6A",
        "ctrlJson": {"if_run": "1"},
    }
    _serve(mock_api, [_scene(300004, "8", devices=False, iuList=[unit])])
    await _setup(hass, config_entry)
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.run_scene.await_args.args[2:] == (
        [],
        [
            {
                "iuCtrlInfo": [
                    {
                        "systemNo": "00",
                        "iuSn": "S1",
                        "iuNo": "01",
                        "iuType": "6A",
                        "ctrlJson": {"if_run": "1"},
                    }
                ],
                "ouCtrlInfo": [],
                "iezCode": "G1",
                "deviceType": "6",
            }
        ],
    )


@pytest.mark.parametrize(
    ("answers", "warning"),
    [
        # A failure code ends it with the cloud's message.
        ([{"code": "206", "message": "设备离线", "data": []}], "设备离线"),
        # Never final: after 20 s a module result "2" still counts as done...
        ([{"code": "205", "message": "", "data": [{"moduleResult": "2"}]}] * 10, None),
        # ...and without one the run is not confirmed.
        ([{"code": "205", "message": "", "data": [{"moduleResult": "1"}]}] * 10, "confirmed"),
        ([{"code": "500", "message": "", "data": None}] * 10, "confirmed"),
    ],
)
async def test_run_result_outcomes(
    hass: HomeAssistant, mock_api, config_entry, fast_results, caplog, answers, warning
) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    mock_api.get_control_results.side_effect = answers
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_control_results.await_count == len(answers)
    assert len(answers) <= SCENE_RESULT_POLLS
    warnings = _warnings(caplog)
    if warning is None:
        assert not warnings
    else:
        assert len(warnings) == 1 and warning in warnings[0]


async def test_run_result_not_read_on_error_or_yield(
    hass: HomeAssistant, mock_api, config_entry, fast_results, caplog
) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, BUTTON_DOMAIN, 300004)
    mock_api.get_control_results.side_effect = YorkConnectionError("timeout")
    await _press(hass, entity_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_control_results.await_count == 1
    assert not _warnings(caplog)

    # No result requests while the session is left to the app.
    mock_api.get_control_results.reset_mock(side_effect=True)
    mock_api.run_scene.return_value = []  # nothing to follow
    await _press(hass, entity_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.get_control_results.assert_not_called()
    mock_api.run_scene.return_value = [{"iezCode": CODE}]
    with patch.object(config_entry.runtime_data, "_yielded_until", 0.0):
        await config_entry.runtime_data._scene_result("x", [{"iezCode": CODE}])
    mock_api.get_control_results.assert_not_called()


async def test_unload_stops_result_check(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    mock_api.get_control_results.assert_not_called()


async def test_run_scene_with_offline_controller(
    hass: HomeAssistant, mock_api, config_entry, fast_results
) -> None:
    """Unlike a command, the app sends a scene without checking who is online.

    The cloud then reports the offline device through multiCtrlResult.
    """
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_online.return_value = {}  # missing counts as offline
    coordinator._last_online = None
    await coordinator.async_refresh()
    assert not coordinator.devices[CODE].online
    with pytest.raises(HomeAssistantError):
        await coordinator.async_request_change(CODE, power=False)
    mock_api.control.assert_not_called()
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    mock_api.run_scene.assert_awaited_once()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_run_scene_rejected(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    mock_api.run_scene.side_effect = YorkCommandError("设备不在线")
    with pytest.raises(HomeAssistantError) as info:
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    assert info.value.translation_key == "scene_rejected"
    assert info.value.translation_placeholders["message"] == "设备不在线"
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.get_control_results.assert_not_called()


async def test_run_scene_errors_map_like_commands(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.run_scene.side_effect = YorkConnectionError("timeout")
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_run_scene(HOME, "300004")
    assert info.value.translation_key == "cannot_connect"
    # Taken again right after the forced login: leave it to the app.
    mock_api.run_scene.side_effect = YorkSessionTakenError("HTTP 401")
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_run_scene(HOME, "300004")
    assert info.value.translation_key == "session_taken"
    assert coordinator.session_yielded
    mock_api.run_scene.side_effect = YorkClientOutdatedError("1.9.0", None)
    coordinator._end_yield()
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_run_scene(HOME, "300004")
    assert info.value.translation_key == "client_outdated"
    assert coordinator.client_outdated


async def test_run_once_scene(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Greenhouse / party: the cloud runs them; the token is the correctly spelt field."""
    greenhouse = _scene(300010, "G")
    _serve(mock_api, [greenhouse])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.run_recommended_scene.return_value = {"updateTime": 77, "upadteTime": 78}
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300010))
        mock_api.run_recommended_scene.assert_awaited_once_with(
            HOME, "G", 300010, greenhouse["updateTime"]
        )
        assert coordinator.homes[HOME].scenes["300010"].record["updateTime"] == 77
        mock_api.run_recommended_scene.return_value = {"upadteTime": 79}
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300010))
        assert coordinator.homes[HOME].scenes["300010"].record["updateTime"] == 77
    coordinator._cancel_control_refresh()
    mock_api.run_scene.assert_not_called()


async def test_run_without_devices_refused(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [_scene(300004, "8", devices=False), _scene(300005, "I", devices=False)])
    await _setup(hass, config_entry)
    for smart_id in (300004, 300005):
        with pytest.raises(ServiceValidationError) as info:
            await _press(hass, _entity(hass, BUTTON_DOMAIN, smart_id))
        assert info.value.translation_key == "scene_no_devices"
    mock_api.run_scene.assert_not_called()
    mock_api.run_recommended_scene.assert_not_called()


async def test_turn_off_scene_without_devices(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The home page lets a running scene without devices be switched off."""
    _serve(mock_api, [_scene(300004, "5", devices=False, openFlg="1")])
    await _setup(hass, config_entry)
    await _switch(hass, _entity(hass, SWITCH_DOMAIN, 300004), False)
    mock_api.set_scene_open.assert_awaited_once()
    assert mock_api.set_scene_open.await_args.args[2] == "0"


async def test_run_releases_held_values(
    hass: HomeAssistant, mock_api, config_entry, fast_results
) -> None:
    """A scene's commands are not masked by values held from an earlier command."""
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await coordinator.async_request_change(CODE, temperature=24.0)
    assert coordinator.devices[CODE].expected
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    assert not coordinator.devices[CODE].expected
    await hass.async_block_till_done(wait_background_tasks=True)


# ================================================================== session


async def test_scene_action_during_yield_reclaims(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    timer = _scene(300004, "O")
    records = [timer]
    _serve(mock_api, records)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    order = []
    mock_api.relogin.side_effect = lambda *a, **k: order.append("relogin") or mock_api.auth
    reads = mock_api.get_scenes.await_count

    def read(home):
        order.append("read")
        return copy.deepcopy(records)

    mock_api.get_scenes.side_effect = read
    mock_api.set_scene_open.side_effect = lambda *args: order.append("send") or {}
    # Meanwhile the scene got a new token in the app.
    records[0] = {**timer, "updateTime": 1700000999999}
    await _switch(hass, _entity(hass, SWITCH_DOMAIN, 300004), True)
    assert order[:3] == ["relogin", "read", "send"]
    assert mock_api.set_scene_open.await_args.args == (HOME, 300004, "1", 1700000999999)
    assert not coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is None
    assert mock_api.get_scenes.await_count >= reads + 1


async def test_yield_reread_finds_scene_gone(hass: HomeAssistant, mock_api, config_entry) -> None:
    records = [_scene(300004, "8")]
    _serve(mock_api, records)
    await _setup(hass, config_entry)
    await _yield(hass, mock_api, config_entry)
    records.clear()  # deleted in the app meanwhile
    with pytest.raises(HomeAssistantError) as info:
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    assert info.value.translation_key == "scene_gone"
    mock_api.run_scene.assert_not_called()


async def test_yield_reread_fails(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    await _yield(hass, mock_api, config_entry)
    mock_api.get_scenes.side_effect = YorkConnectionError("timeout")
    with pytest.raises(HomeAssistantError) as info:
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    assert info.value.translation_key == "cannot_connect"
    mock_api.run_scene.assert_not_called()


async def test_no_scene_requests_during_yield(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    reads = mock_api.get_scenes.await_count
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == reads
    # Logging in again after the yield reads the scenes again at once.
    with _later(31 * 60):
        await coordinator.async_refresh()
    mock_api.relogin.assert_awaited_once()
    assert mock_api.get_scenes.await_count == reads + 1


async def test_command_reread_skips_scenes(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Taking the session back for a climate command reads no scene list."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    reads = mock_api.get_scenes.await_count
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await coordinator.async_request_change(CODE, power=False)
    assert mock_api.get_scenes.await_count == reads
    coordinator._cancel_control_refresh()


# ============================================================ slow scene timer


async def test_scene_fetch_failure_keeps_climate(
    hass: HomeAssistant, mock_api, config_entry, caplog
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.york_smart_home")
    timer = _scene(300004, "O", openFlg="1")
    _serve(mock_api, [timer])
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    switch_id = _entity(hass, SWITCH_DOMAIN, 300004)
    ts = FIXTURE["status"]["timestamp"]

    mock_api.get_scenes.side_effect = YorkConnectionError("smarts: HTTP 502")
    heat = {
        **FIXTURE["status"],
        "timestamp": ts + 1000,
        "roundTime": ts + 1000,
        "wire_controller_makecold": "0",
        "wire_controller_heating": "1",
    }
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(heat)}
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get(entity_id).state == "heat"  # the climate poll went on
    assert hass.states.get(switch_id).state == "on"  # the last scene list is kept
    assert coordinator.scene_error == "smarts: HTTP 502"
    with _later(2 * SCENE_INTERVAL + 2):
        await coordinator.async_refresh()
    warnings = _warnings(caplog)
    assert len(warnings) == 1  # logged once
    assert "keeping the last ones" in warnings[0]
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["scene_read_failed"] is True

    _serve(mock_api, [{**timer, "openFlg": "0"}])
    with _later(3 * SCENE_INTERVAL + 3):
        await coordinator.async_refresh()
    assert coordinator.scene_error is None
    assert hass.states.get(switch_id).state == "off"
    assert "can be read again" in caplog.text


async def test_scene_fetch_api_error_keeps_scenes(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = YorkApiError("smarts: code 500 系统异常")
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "off"
    # Retried at the next slow tick, not at every poll.
    calls = mock_api.get_scenes.await_count
    with _later(SCENE_INTERVAL + 3):
        await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == calls


async def test_scene_fetch_session_taken_yields(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = YorkSessionTakenError("HTTP 401")
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert coordinator.session_yielded
    assert _issue(hass, config_entry, "session_taken") is not None
    mock_api.relogin.assert_not_called()


async def test_scene_fetch_rate_limited(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = YorkRateLimitError(300)
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
        calls = mock_api.get_status.await_count
        await coordinator.async_refresh()
    assert mock_api.get_status.await_count == calls  # paused like any 429


async def test_scene_fetch_client_outdated(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = YorkClientOutdatedError("1.9.0", None)
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert coordinator.client_outdated
    assert _issue(hass, config_entry, "client_outdated") is not None


async def test_scene_cadence(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The scene list is read on its own slow timer, while the 2 s polling runs."""
    await _setup(hass, config_entry)
    assert mock_api.get_scenes.await_count == 1  # at setup
    status_calls = mock_api.get_status.await_count
    now = dt_util.utcnow()
    for i in range(1, 6):
        async_fire_time_changed(hass, now + timedelta(seconds=2.1 * i))
        await hass.async_block_till_done()
    assert mock_api.get_status.await_count - status_calls == 5  # the timer fires
    assert mock_api.get_scenes.await_count == 1
    with _later(SCENE_INTERVAL - 5):
        async_fire_time_changed(hass, now + timedelta(seconds=2.1 * 6))
        await hass.async_block_till_done()
    assert mock_api.get_scenes.await_count == 1
    with _later(SCENE_INTERVAL + 1):
        async_fire_time_changed(hass, now + timedelta(seconds=2.1 * 7))
        await hass.async_block_till_done()
        assert mock_api.get_scenes.await_count == 2
        async_fire_time_changed(hass, now + timedelta(seconds=2.1 * 8))
        await hass.async_block_till_done()
    assert mock_api.get_scenes.await_count == 2
    assert mock_api.get_status.await_count - status_calls == 8


async def test_scene_action_triggers_read(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [_scene(300004, "O")])
    await _setup(hass, config_entry)
    reads = mock_api.get_scenes.await_count
    status_calls = mock_api.get_status.await_count
    await _switch(hass, _entity(hass, SWITCH_DOMAIN, 300004), True)
    assert mock_api.get_scenes.await_count == reads
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 1
    assert mock_api.get_status.await_count == status_calls + 1  # status refresh too
    # Then the slow timer again.
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 1


async def test_scenes_added_and_removed(hass: HomeAssistant, mock_api, config_entry) -> None:
    records = [copy.deepcopy(SCENES[0])]
    _serve(mock_api, records)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    manual = _scene(300004, "8")
    records.append(manual)
    assert _entity(hass, BUTTON_DOMAIN, 300004) is None
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    button_id = _entity(hass, BUTTON_DOMAIN, 300004)
    assert hass.states.get(button_id).state == "unknown"  # created in the app: new entity

    # Renamed in the app.
    records[1] = {**manual, "smartName": "回家前开空调"}
    with _later(2 * SCENE_INTERVAL + 2):
        await coordinator.async_refresh()
    assert hass.states.get(button_id).attributes["friendly_name"].endswith("回家前开空调")

    # Deleted in the app: unavailable, and pressing it is refused.
    records.pop()
    with _later(3 * SCENE_INTERVAL + 3):
        await coordinator.async_refresh()
    assert hass.states.get(button_id).state == "unavailable"
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "off"
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_run_scene(HOME, "300004")
    assert info.value.translation_key == "scene_gone"

    # Back again.
    records.append(manual)
    with _later(4 * SCENE_INTERVAL + 4):
        await coordinator.async_refresh()
    assert hass.states.get(button_id).state == "unknown"


async def test_home_without_controller(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Scenes are read per home with a supported controller only."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    reads = mock_api.get_scenes.await_count
    mock_api.get_wire_controllers.return_value = []
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "unavailable"
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == reads


async def test_two_homes(hass: HomeAssistant, mock_api, config_entry) -> None:
    second = copy.deepcopy(FIXTURE["device"])
    second.update(xkqCode="0000000000000002", homeId=100004, homeName="别墅")
    mock_api.get_wire_controllers.return_value = [copy.deepcopy(FIXTURE["device"]), second]
    mock_api.get_status.side_effect = lambda home, codes: {
        c: copy.deepcopy(FIXTURE["status"]) for c, _ in codes
    }
    mock_api.get_online.return_value = {CODE: True, "0000000000000002": True}
    other = _scene(300004, "8", homeId=100004)

    def scenes(home):
        if home == 100004:
            return [copy.deepcopy(other)]
        raise YorkApiError("smarts: code 500")

    mock_api.get_scenes.side_effect = scenes
    await _setup(hass, config_entry)
    assert sorted(c.args[0] for c in mock_api.get_scenes.await_args_list) == [HOME, 100004]
    # One home failing does not keep the other's scenes away.
    button_id = _entity(hass, BUTTON_DOMAIN, 300004, home=100004)
    assert hass.states.get(button_id).state == "unknown"
    assert _entity(hass, SWITCH_DOMAIN, SLEEP_ID) is None
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(button_id).device_id)
    assert device.name == "别墅 York Smart Home"
    await _press(hass, button_id)
    assert mock_api.run_scene.await_args.args[0] == 100004


async def test_scene_diagnostics(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, [copy.deepcopy(SCENES[0]), _scene(300004, "8")])
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["scene_read_failed"] is False
    (home,) = diag["homes"]
    assert home["home"] == 1 and home["scenes_loaded"] and home["present"]
    sleep, manual = home["scenes"]
    assert sleep == {
        "type": "5",
        "entity": "switch",
        "present": True,
        "openFlg": "0",
        "actionFlg": None,
        "favoriteFlg": "0",
        "wire_controllers": [],
        "indoor_unit_count": 0,
    }
    assert manual["entity"] == "button"
    assert manual["wire_controllers"] == [
        {"xkqType": "9", "deviceType": "C", "ctrlJson": XKQ_ENTRY["ctrlJson"]}
    ]
    text = repr(diag)
    for secret in (str(SLEEP_ID), "300004", "睡眠", "场景8", str(HOME), CODE, str(UPDATE_TIME)):
        assert secret not in text, secret
