"""Cloud scenes (智能场景): parsing, entities, requests, session handling, cadence."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
from datetime import timedelta
import json
import logging
from pathlib import Path
import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.york_smart_home.api import (
    HomeScenes,
    Session,
    YorkApiError,
    YorkAuthError,
    YorkClientOutdatedError,
    YorkCommandError,
    YorkConnectionError,
    YorkRateLimitError,
    YorkSceneConflictError,
    YorkSessionConflictError,
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
    has_sensor,
    js_equals,
    js_truthy,
    module_succeeded,
    replay_request,
    result_outcome,
    run_ctrl_type,
    scene_device_count,
    scene_is_on,
    scene_name,
    scene_offered,
    scene_status,
    scene_target_reached,
    scene_type,
)
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import CODE, FIXTURE, SCENE_FLAGS, SCENES
from .test_api import OK, UNAUTHORIZED, _api, _login_body, _resp, _sequence
from .test_integration import _setup
from .test_session import _issue, _later, _reauth_started, _yield

HOME = 100001
SLEEP_ID = 300003
UPDATE_TIME = 1700000000002
BASE = Path(__file__).parent.parent / "custom_components" / DOMAIN
# A fake air box (约克小方) code.
BOX = "box-0000-0001"

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

# The account's flags (SCENE_FLAGS) have only the vacation feature on; a
# home with every scene feature switched on.
ALL_FEATURES = {
    **SCENE_FLAGS,
    "leaveEnergyFlg": True,
    "nightQuiteFlg": True,
    "kszrFlg": "1",
    **{f"scene{letter}Flg": "1" for letter in "GHIJKL"},
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


def _serve(mock_api, records: list[dict], flags: dict | None = None) -> None:
    """Serve ``records`` as the home's scene list (with the account's flags)."""
    mock_api.get_scenes.side_effect = lambda home, **kwargs: HomeScenes(
        copy.deepcopy(records), dict(SCENE_FLAGS if flags is None else flags)
    )


class _Cloud:
    """The home's scene list as the cloud keeps it: accepted changes update it.

    Like the cloud, a change must carry the scene's current updateTime (an
    optimistic lock), and every change, in the app or from HA, gives the
    scene a new one.
    """

    def __init__(self, mock_api, records: list[dict], flags: dict | None = None) -> None:
        self.records = records
        self.flags = dict(SCENE_FLAGS if flags is None else flags)
        self.token = 1700000900000
        mock_api.get_scenes.side_effect = self.read
        mock_api.set_scene_open.side_effect = self.set_open
        mock_api.run_recommended_scene.side_effect = self.recommended
        mock_api.set_auto_scene.side_effect = self.set_auto

    def read(self, home, **kwargs) -> HomeScenes:
        return HomeScenes(copy.deepcopy(self.records), dict(self.flags))

    def _record(self, smart_id) -> dict:
        return next(r for r in self.records if str(r["smartId"]) == str(smart_id))

    def edit(self, smart_id, **fields) -> None:
        """A change made in the app."""
        record = self._record(smart_id)
        record.update(fields)
        self.token += 1
        record["updateTime"] = self.token

    def _change(self, smart_id, update_time, key, value) -> dict:
        record = self._record(smart_id)
        if update_time != record.get("updateTime"):
            raise YorkCommandError("数据已被修改，请刷新")
        if key is not None:
            record[key] = value
        self.token += 1
        record["updateTime"] = self.token
        return {"updateTime": self.token}

    def set_open(self, home, smart_id, flag, update_time) -> dict:
        return self._change(smart_id, update_time, "openFlg", flag)

    def recommended(self, home, scene_type_code, smart_id, update_time, *, open_flg=None) -> dict:
        return self._change(smart_id, update_time, "openFlg" if open_flg else None, open_flg)

    def set_auto(self, home, smart_id, scene_type_code, flag, update_time) -> dict:
        return self._change(smart_id, update_time, "actionFlg", flag)


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


def _heat_status() -> dict:
    """The fixture status, newer and in heating mode."""
    ts = FIXTURE["status"]["timestamp"]
    return {
        **FIXTURE["status"],
        "timestamp": ts + 1000,
        "roundTime": ts + 1000,
        "wire_controller_makecold": "0",
        "wire_controller_heating": "1",
    }


# ===================================================================== rules


def test_scene_types_like_the_app() -> None:
    """The scenes tab's control per type (template chunk 18, ScenesPage chunk 15)."""
    runs = {code for code, stype in SCENE_TYPES.items() if stype.control == RUN}
    switches = {code for code, stype in SCENE_TYPES.items() if stype.control == SWITCH}
    assert runs == {"1", "2", "8", "F", "G", "I"}
    assert switches == {"5", "6", "7", "9", "B", "C", "D", "E", "H", "J", "K", "L", "M", "O"}
    # Replayed through multiIuOuCtrl: the scenes tab's ctrlType, and the
    # home page's own for leave / return home.
    replays = {
        code: (s.ctrl_type, s.home_ctrl_type)
        for code, s in SCENE_TYPES.items()
        if s.request == REPLAY
    }
    assert replays == {
        "1": ("YO438", "YO431"),
        "2": ("YO438", "YO432"),
        "8": ("YO438", None),
        "F": ("YO43F", None),
    }
    by_request = {
        request: {code for code, s in SCENE_TYPES.items() if s.request == request}
        for request in (OPEN_FLAG, RECOMMENDED, AUTO)
    }
    assert by_request == {
        OPEN_FLAG: {"5", "6", "7", "9", "M", "O"},
        RECOMMENDED: {"G", "I", "J", "K", "L"},
        AUTO: {"B", "C", "D", "E", "H"},
    }
    # Shown by a feature flag of the home detail (initRecommendSceneFakeData).
    assert {code: s.feature for code, s in SCENE_TYPES.items() if s.feature} == {
        "6": "leaveEnergyFlg",
        "7": "nightQuiteFlg",
        "F": "kszrFlg",
        "G": "sceneGFlg",
        "H": "sceneHFlg",
        "I": "sceneIFlg",
        "J": "sceneJFlg",
        "K": "sceneKFlg",
        "L": "sceneLFlg",
    }
    # The air box field runAutoScenes checks.
    assert {code: s.sensor_key for code, s in SCENE_TYPES.items() if s.sensor_key} == {
        "B": "boxCode",
        "C": "boxCode",
        "D": "boxCode",
        "H": "boxCode",
        "E": "boxCodes",
    }
    # Never listed by the app: no entity.
    for code in ("3", "4", "A", "N", "Z", "X", "", None):
        assert scene_type({"type": code}) is None
    assert scene_type({}) is None
    # The type is compared as text (8 and "8" alike).
    assert scene_type({"type": 8}) is SCENE_TYPES["8"]


def test_javascript_comparisons() -> None:
    """The app's flag tests: truthiness and `"1" == flag`."""
    for value in (True, 1, 2.5, "1", "0", "x", [], {}):
        assert js_truthy(value), value
    for value in (False, 0, 0.0, float("nan"), "", None):
        assert not js_truthy(value), value
    for value in ("1", 1, 1.0, True):
        assert js_equals(value, "1"), value
    for value in ("0", " 1", "01", "true", 0, 2, False, None, [1], {"1": 1}):
        assert not js_equals(value, "1"), value
    assert js_equals("0", "0") and js_equals(0, "0") and js_equals(False, "0")
    assert not js_equals(None, "0") and not js_equals("", "0")


def test_scene_offered() -> None:
    """Shown like the scenes tab: some types only with their feature on."""
    for code in "12589BCDEMO":
        assert scene_offered({"type": code}, {}), code
    for code in ("3", "4", "A", "N", "Z", None):
        assert not scene_offered({"type": code}, ALL_FEATURES), code
    gated = "67FGHIJKL"
    # The account's home: only the vacation feature is on.
    assert {code for code in gated if scene_offered({"type": code}, SCENE_FLAGS)} == {"K"}
    assert all(scene_offered({"type": code}, ALL_FEATURES) for code in gated)
    # A missing flag hides the scene, as in the app.
    assert not any(scene_offered({"type": code}, {}) for code in gated)
    # leaveEnergyFlg / nightQuiteFlg are booleans, tested for truthiness...
    for value, shown in (
        (True, True),
        (False, False),
        (None, False),
        (1, True),
        (0, False),
        ("0", True),
        ("", False),
    ):
        assert scene_offered({"type": "6"}, {"leaveEnergyFlg": value}) is shown, value
        assert scene_offered({"type": "7"}, {"nightQuiteFlg": value}) is shown, value
    # ...the others compared with "1" == flag.
    for value, shown in (
        ("1", True),
        (1, True),
        (True, True),
        ("0", False),
        (0, False),
        (None, False),
        ("true", False),
    ):
        assert scene_offered({"type": "K"}, {"sceneKFlg": value}) is shown, value
        assert scene_offered({"type": "F"}, {"kszrFlg": value}) is shown, value


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
    # Linkage and pets scenes keep their state in actionFlg; HANGUP is running
    # (the linkage list's toggle shows it as off, but a tap sends "0").
    pets = SCENE_TYPES["H"]
    assert scene_is_on({"actionFlg": "1", "openFlg": "0"}, pets)
    assert scene_is_on({"actionFlg": "2"}, pets)
    assert scene_is_on({"actionFlg": "2"}, SCENE_TYPES["B"])
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


def test_air_box() -> None:
    """runAutoScenes: boxCode (B, C, D, H) or boxCodes (E); home page: boxList."""
    summer, purify, pets, sleep = (SCENE_TYPES[code] for code in "BEH5")
    assert not has_sensor({"boxCode": None}, summer)
    assert not has_sensor({"boxCode": ""}, summer)
    assert not has_sensor({}, pets)
    assert has_sensor({"boxCode": BOX}, summer)
    assert has_sensor({"boxCode": BOX}, pets)
    # Purification checks boxCodes (runAutoScenes' else branch).
    assert not has_sensor({"boxCode": BOX, "boxCodes": None}, purify)
    assert has_sensor({"boxCodes": BOX}, purify)
    # The home page refuses a scene whose boxList is there but empty.
    assert not has_sensor({"boxCode": BOX, "boxList": []}, summer)
    assert has_sensor({"boxCode": BOX, "boxList": [{"boxCode": BOX}]}, summer)
    assert has_sensor({"boxCode": BOX, "boxList": None}, summer)
    # Other types have no air box.
    assert has_sensor({}, sleep)
    assert has_sensor({"boxList": []}, SCENE_TYPES["8"])


def test_scene_name() -> None:
    assert scene_name(SCENES[0]) == "睡眠"
    assert scene_name({"type": "5", "smartName": "  "}) == "睡眠曲线"
    assert scene_name({"type": "8", "smartName": None}) == "手动场景"
    assert scene_name({"type": "Z", "smartId": 300099}) == "300099"


def test_run_ctrl_type() -> None:
    """Leave / return home: the home page's ctrlType only where it shows the button."""
    leave, back, manual = SCENE_TYPES["1"], SCENE_TYPES["2"], SCENE_TYPES["8"]
    favourite = {"favoriteFlg": "1"}
    assert run_ctrl_type(favourite, leave, {"scene2Flg": "1"}) == "YO431"
    assert run_ctrl_type(favourite, back, {"scene2Flg": 1}) == "YO432"
    # The home page drops "0" == favoriteFlg: then only the scenes tab runs it.
    assert run_ctrl_type({"favoriteFlg": "0"}, leave, {"scene2Flg": "1"}) == "YO438"
    assert run_ctrl_type({"favoriteFlg": 0}, back, {"scene2Flg": "1"}) == "YO438"
    assert run_ctrl_type({}, leave, {"scene2Flg": "1"}) == "YO431"
    # Without scene2Flg "1" the home page shows neither.
    assert run_ctrl_type(favourite, leave, {"scene2Flg": "0"}) == "YO438"
    assert run_ctrl_type(favourite, back, {}) == "YO438"
    assert run_ctrl_type(favourite, manual, {"scene2Flg": "1"}) == "YO438"


def test_replay_wire_controllers() -> None:
    """xkqCtrlList is the scene's xkqList, entries copied as stored."""
    record = _scene(300004, "8")
    record["xkqList"].append({"xkqCode": "0000000000000002", "xkqType": "8", "ctrlJson": "{}"})
    request = replay_request(record, SCENE_TYPES["8"], SCENE_FLAGS)
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
        favourite = _scene(300005, code, favoriteFlg="1")
        assert replay_request(favourite, SCENE_TYPES[code], SCENE_FLAGS)["ctrl_type"] == ctrl_type
    fast_heat = replay_request(_scene(300006, "F", devices=False), SCENE_TYPES["F"], ALL_FEATURES)
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
    assert replay_request(record, SCENE_TYPES["8"], SCENE_FLAGS)["ctrl_list"] == [
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
        exceptions = strings["exceptions"]
        for key in (
            "scene_gone",
            "scene_hidden",
            "scene_no_devices",
            "scene_no_sensor",
            "scene_conflict",
            "scene_rejected",
        ):
            assert key in exceptions
        for key in ("scene_hidden", "scene_no_devices", "scene_no_sensor"):
            assert "{scene}" in exceptions[key]["message"], key
        assert "{message}" in exceptions["scene_conflict"]["message"]


# ======================================================================= api

DETAIL_URL = f"{API_BASE}/api/apphome/homes/{HOME}"
LOGIN_URL = f"{API_BASE}/login"
CTRL_URL = f"{API_BASE}/api/appcontrol/cmds/multiIuOuCtrl"
RESULT_URL = f"{API_BASE}/api/appcontrol/cmds/multiCtrlResult"


async def test_get_scenes(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The home detail (FAMILY_DETAIL): its scene records and flags, nothing else."""
    sleep = copy.deepcopy(SCENES[0])
    detail = {
        **SCENE_FLAGS,
        "homeId": HOME,
        "homeName": "家",
        "position1": "某省 某市 某区",
        "positionLat": "0.0",
        "adminPhoneNo": "13800000000",
        "xkqList": [copy.deepcopy(FIXTURE["device"])],
        "deviceList": [],
        "smartList": [sleep, {"smartName": "x"}, "bad"],
    }
    aioclient_mock.get(DETAIL_URL, json={**OK, "data": {"homeDetail": detail}})
    api = _api(hass, auth=Session("tok", 200002))
    result = await api.get_scenes(HOME)
    assert result.scenes == [sleep]  # records without smartId skipped
    assert result.flags == SCENE_FLAGS  # only the feature flags are kept
    (call,) = aioclient_mock.mock_calls
    assert call[0] == "GET"
    assert str(call[1]) == DETAIL_URL


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({**OK, "data": {"homeDetail": {"smartList": []}}}, ([], {})),
        (
            {**OK, "data": {"homeDetail": {"smartList": None, "scene2Flg": "1"}}},
            ([], {"scene2Flg": "1"}),
        ),
        (
            {**OK, "data": {"homeDetail": {"leaveEnergyFlg": False}}},
            ([], {"leaveEnergyFlg": False}),
        ),
        ({**OK, "data": {"homeDetail": None}}, YorkApiError),
        ({**OK, "data": {}}, YorkApiError),
        ({**OK, "data": None}, YorkApiError),
        ({**OK, "data": {"homeDetail": {"smartList": {"a": 1}}}}, YorkApiError),
        ({"code": "500", "message": "系统异常"}, YorkApiError),
    ],
)
async def test_get_scenes_answers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, answer, expected
) -> None:
    aioclient_mock.get(DETAIL_URL, json=answer)
    api = _api(hass, auth=Session("tok", 200002))
    if isinstance(expected, tuple):
        result = await api.get_scenes(HOME)
        assert (result.scenes, result.flags) == expected
    else:
        with pytest.raises(expected):
            await api.get_scenes(HOME)


@pytest.mark.parametrize(
    ("status", "body"), [(403, {}), (200, {"code": "403", "message": "无权限"})]
)
@pytest.mark.parametrize("reclaim", [False, True])
async def test_scene_read_403_is_not_a_login(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body, reclaim
) -> None:
    """A 403 on the optional scene read: an error of that read, never a new login."""
    aioclient_mock.get(DETAIL_URL, side_effect=lambda *a, **k: _resp(body, status))
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    with pytest.raises(YorkApiError, match="403") as info:
        await api.get_scenes(HOME, reclaim=reclaim)
    assert not isinstance(info.value, YorkAuthError)
    assert aioclient_mock.call_count == 1  # no login
    assert api.auth.token == "tok"


async def test_scene_read_401(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The slow read leaves a taken session to the app; a user action takes it back."""
    aioclient_mock.get(
        DETAIL_URL,
        side_effect=_sequence(UNAUTHORIZED, UNAUTHORIZED, {**OK, "data": {"homeDetail": {}}}),
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    with pytest.raises(YorkSessionTakenError):
        await api.get_scenes(HOME)
    assert aioclient_mock.call_count == 1  # no login
    assert api.auth.token == "tok"
    assert (await api.get_scenes(HOME, reclaim=True)).scenes == []
    assert api.auth.token == "fresh"
    detail, login = f"/api/apphome/homes/{HOME}", "/login"
    assert [call[1].path for call in aioclient_mock.mock_calls] == [detail, detail, login, detail]


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
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
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
        side_effect=_sequence(
            {"code": "205", "message": "执行中", "data": []},
            UNAUTHORIZED,
            {"code": "403", "message": "无权限"},
        ),
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    answer = await api.get_control_results(tokens)
    assert answer["code"] == "205"  # the envelope, whatever the code
    assert aioclient_mock.mock_calls[0][2] == {"tokenList": tokens}
    # A background check never logs in: not on a 401, not on a 403.
    with pytest.raises(YorkSessionTakenError):
        await api.get_control_results(tokens)
    with pytest.raises(YorkApiError, match="403"):
        await api.get_control_results(tokens)
    assert aioclient_mock.call_count == 3
    assert api.auth.token == "tok"


@pytest.mark.parametrize("read", ["list", "result"])
async def test_background_reads_with_always_reclaim(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, read
) -> None:
    """With "always reclaim" on, a 401 logs in again on these reads too.

    As for every request (the status polling does the same): the option
    means HA always takes the session back. A 403 still never logs in.
    """
    url, path = (
        (DETAIL_URL, f"/api/apphome/homes/{HOME}")
        if read == "list"
        else (RESULT_URL, "/api/appcontrol/cmds/multiCtrlResult")
    )
    answer = {**OK, "data": {"homeDetail": {}}} if read == "list" else {**OK, "data": []}
    getattr(aioclient_mock, "get" if read == "list" else "post")(
        url, side_effect=_sequence(UNAUTHORIZED, answer, {"code": "403", "message": "无权限"})
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002), reclaim_session=True)

    def call():
        return api.get_scenes(HOME) if read == "list" else api.get_control_results([])

    await call()
    assert api.auth.token == "fresh"
    with pytest.raises(YorkApiError, match="403"):
        await call()
    assert [c[1].path for c in aioclient_mock.mock_calls] == [path, "/login", path, path]


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


async def test_scene_changes_without_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A record without updateTime: the key is left out, like the app's undefined."""
    aioclient_mock.put(f"{API_BASE}/api/apptimer/homes/{HOME}/smarts/{SLEEP_ID}/openFlg", json=OK)
    aioclient_mock.post(f"{API_BASE}/api/apptimer/homes/{HOME}/scene/type/K/300011", json=OK)
    aioclient_mock.put(
        f"{API_BASE}/api/apptimer/homes/{HOME}/newScenes/300013/flag/0",
        json={**OK, "data": {"updateTime": 9}},
    )
    api = _api(hass, auth=Session("tok", 200002))
    await api.set_scene_open(HOME, SLEEP_ID, "1", None)
    await api.run_recommended_scene(HOME, "K", 300011, None, open_flg="1")
    await api.set_auto_scene(HOME, 300013, "B", "1", None)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert bodies[0] == {"openFlg": "1"}
    assert bodies[1] == {
        "homeId": HOME,
        "sceneId": 300011,
        "openFlg": "1",
        "type": "K",
        "forceOpenFlg": "0",
    }
    assert "updateTime" not in bodies[2] and bodies[2]["actionFlg"] == "1"
    # A known token is sent as it is, 0 too.
    await api.set_scene_open(HOME, SLEEP_ID, "0", 0)
    assert aioclient_mock.mock_calls[-1][2] == {"openFlg": "0", "updateTime": 0}


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
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("old", 200002))
    await api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME)
    assert api.auth.token == "fresh"


# Every scene request of a user action: method, path, the call.
SCENE_ACTIONS = {
    "open_flag": (
        "put",
        f"/api/apptimer/homes/{HOME}/smarts/{SLEEP_ID}/openFlg",
        lambda api: api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME),
    ),
    "recommended": (
        "post",
        f"/api/apptimer/homes/{HOME}/scene/type/K/300011",
        lambda api: api.run_recommended_scene(HOME, "K", 300011, UPDATE_TIME, open_flg="1"),
    ),
    "run_once": (
        "post",
        f"/api/apptimer/homes/{HOME}/scene/type/G/300010",
        lambda api: api.run_recommended_scene(HOME, "G", 300010, UPDATE_TIME),
    ),
    "auto": (
        "put",
        f"/api/apptimer/homes/{HOME}/newScenes/300013/flag/0",
        lambda api: api.set_auto_scene(HOME, 300013, "B", "1", UPDATE_TIME),
    ),
    "replay": (
        "post",
        "/api/appcontrol/cmds/multiIuOuCtrl",
        lambda api: api.run_scene(HOME, "YO438", [], []),
    ),
}


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (403, {}, "HTTP 403"),
        (200, {"code": "403", "message": "无权限"}, "无权限"),
        (200, {"code": 403}, "code 403"),
    ],
)
@pytest.mark.parametrize("action", SCENE_ACTIONS)
async def test_scene_action_403_is_a_rejection(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body, message, action
) -> None:
    """A 403 on a scene change or run refuses the action, not the session.

    The action has just read the home's scene list with the same token
    (YorkCoordinator._prepare_scene): never a new login, which would log
    the phone app out and then ask to log in again although the
    credentials are fine.
    """
    method, path, call = SCENE_ACTIONS[action]
    getattr(aioclient_mock, method)(
        f"{API_BASE}{path}", side_effect=lambda *a, **k: _resp(body, status)
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    with pytest.raises(YorkCommandError) as info:
        await call(api)
    assert str(info.value) == message  # the server's text, never the path
    assert not isinstance(info.value, YorkSceneConflictError)
    assert aioclient_mock.call_count == 1  # no login
    assert api.auth.token == "tok"


async def test_scene_action_403_after_reclaim(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Taken back from the app for the action (401), then refused: one login only."""
    path = f"/api/apptimer/homes/{HOME}/smarts/{SLEEP_ID}/openFlg"
    aioclient_mock.put(
        f"{API_BASE}{path}",
        side_effect=_sequence(UNAUTHORIZED, {"code": "403", "message": "无权限"}),
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("old", 200002))
    with pytest.raises(YorkCommandError, match="^无权限$"):
        await api.set_scene_open(HOME, SLEEP_ID, "1", UPDATE_TIME)
    assert api.auth.token == "fresh"
    assert [call[1].path for call in aioclient_mock.mock_calls] == [path, "/login", path]


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
    _serve(mock_api, [])
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    without = {e.unique_id for e in registry.entities.values() if e.platform == DOMAIN}
    assert with_scene - without == {f"home_{HOME}_scene_{SLEEP_ID}"}
    assert without < with_scene
    assert all(uid == CODE or uid.startswith(f"{CODE}_") for uid in without)


async def test_entities_per_type(hass: HomeAssistant, mock_api, config_entry) -> None:
    codes = "123456789ABCDEFGHIJKLMNOZ"
    records = [_scene(300100 + i, code) for i, code in enumerate(codes)]
    _serve(mock_api, records, ALL_FEATURES)
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


async def test_hidden_scenes(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Like the app, a scene whose feature is off for the home gets no entity."""
    gated = "67FGHIJKL"
    records = [copy.deepcopy(SCENES[0])] + [
        _scene(300100 + i, code) for i, code in enumerate(gated)
    ]
    flags = dict(SCENE_FLAGS)  # the account's: only vacation (K) is on
    _serve(mock_api, records, flags)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    def made() -> set[str]:
        return {
            code
            for i, code in enumerate(gated)
            if _entity(hass, BUTTON_DOMAIN, 300100 + i) or _entity(hass, SWITCH_DOMAIN, 300100 + i)
        }

    assert made() == {"K"}
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "off"

    # The cloud switches the greenhouse and leave-home energy saving features on.
    flags.update(sceneGFlg="1", leaveEnergyFlg=True)
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert made() == {"K", "G", "6"}
    assert hass.states.get(_entity(hass, BUTTON_DOMAIN, 300103)).state == "unknown"
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, 300100)).state == "off"

    # ...and the vacation feature off: unavailable, and refused without a request.
    flags["sceneKFlg"] = "0"
    with _later(2 * SCENE_INTERVAL + 2):
        await coordinator.async_refresh()
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, 300107)).state == "unavailable"
    reads = mock_api.get_scenes.await_count
    with pytest.raises(ServiceValidationError) as info:
        await coordinator.async_set_scene(HOME, "300107", True)
    assert info.value.translation_key == "scene_hidden"
    assert info.value.translation_placeholders == {"scene": "场景K"}
    assert mock_api.get_scenes.await_count == reads
    mock_api.run_recommended_scene.assert_not_called()


async def test_sleep_scene_without_devices(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Like the app: a scene without devices is not switched on, nothing is sent.

    Decided on the list read right before, not on the cached one.
    """
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, SLEEP_ID)
    scenes_read = mock_api.get_scenes.await_count
    with pytest.raises(ServiceValidationError) as info:
        await _switch(hass, entity_id, True)
    assert info.value.translation_key == "scene_no_devices"
    assert info.value.translation_placeholders == {"scene": "睡眠"}
    assert mock_api.get_scenes.await_count == scenes_read + 1
    await _switch(hass, entity_id, False)  # already off
    await hass.async_block_till_done()
    assert mock_api.get_scenes.await_count == scenes_read + 2
    mock_api.set_scene_open.assert_not_called()
    mock_api.relogin.assert_not_called()
    assert hass.states.get(entity_id).state == "off"


async def test_devices_added_in_app(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The README's flow: add the controller to the sleep scene in the app, switch it on in HA."""
    sleep = copy.deepcopy(SCENES[0])
    cloud = _Cloud(mock_api, [sleep])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, SLEEP_ID)
    cloud.edit(SLEEP_ID, xkqList=[copy.deepcopy(XKQ_ENTRY)])
    token = sleep["updateTime"]
    with _no_refresh(config_entry):
        await _switch(hass, entity_id, True)
    mock_api.set_scene_open.assert_awaited_once_with(HOME, SLEEP_ID, "1", token)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["devices"] == 1
    assert sleep["openFlg"] == "1"  # the cloud took it
    mock_api.relogin.assert_not_called()


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
    assert mock_api.get_scenes.await_count == reads + 1  # read right before
    assert hass.states.get(entity_id).state == "on"  # shown right away
    # The refresh scheduled by the action reads the list again (the cloud
    # still says off here).
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 2
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


async def test_switched_in_app_since_last_read(hass: HomeAssistant, mock_api, config_entry) -> None:
    """HA decides on the list read right before, not on one up to 10 minutes old."""
    timer = _scene(300004, "O")
    cloud = _Cloud(mock_api, [timer])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    # Switched on in the app since HA's last read: switching off sends "0"
    # with the scene's current token.
    cloud.edit(300004, openFlg="1")
    token = timer["updateTime"]
    assert hass.states.get(entity_id).state == "off"
    with _no_refresh(config_entry):
        await _switch(hass, entity_id, False)
    mock_api.set_scene_open.assert_awaited_once_with(HOME, 300004, "0", token)
    assert timer["openFlg"] == "0"  # the cloud took it
    assert hass.states.get(entity_id).state == "off"
    # On in the app again: switching on sends nothing, and shows it.
    cloud.edit(300004, openFlg="1")
    with _no_refresh(config_entry):
        await _switch(hass, entity_id, True)
    assert mock_api.set_scene_open.await_count == 1
    assert hass.states.get(entity_id).state == "on"


async def test_switch_keeps_answer_token(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The answer's updateTime is the scene's token until the list is read again."""
    cloud = _Cloud(mock_api, [_scene(300004, "5")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    with _no_refresh(config_entry):
        await coordinator.async_set_scene(HOME, "300004", True)
        token = cloud.records[0]["updateTime"]  # the cloud's new one, in the answer
        assert coordinator.homes[HOME].scenes["300004"].record["updateTime"] == token
        # Without updateTime in the answer the token read before stays.
        mock_api.set_scene_open.side_effect = lambda *args: {}
        await coordinator.async_set_scene(HOME, "300004", False)
        assert mock_api.set_scene_open.await_args.args == (HOME, 300004, "0", token)
        record = coordinator.homes[HOME].scenes["300004"].record
        assert record["updateTime"] == token
        assert record["openFlg"] == "0"


async def test_toggle_keeps_list_read_meanwhile(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A list read while the request is out is not reverted by the accepted change."""
    _serve(mock_api, [_scene(300004, "O")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    scene = coordinator.homes[HOME].scenes["300004"]

    def request(*args):
        # The status poll reads the list meanwhile: renamed, with a new token.
        scene.record = {**scene.record, "smartName": "改名", "updateTime": 1700000000777}
        return {}

    mock_api.set_scene_open.side_effect = request
    with _no_refresh(config_entry):
        await coordinator.async_set_scene(HOME, "300004", True)
    assert scene.record["smartName"] == "改名"
    assert scene.record["updateTime"] == 1700000000777
    assert scene.record["openFlg"] == "1"


def _held_poll_reads(mock_api, cloud: _Cloud) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the answers of the status polls' scene list reads.

    The cloud answers when asked (the answer is taken then); it is
    delivered once the returned ``deliver`` event is set. ``out`` is set
    when such a read is under way. A scene action's own read (reclaim)
    is answered right away.
    """
    out, deliver = asyncio.Event(), asyncio.Event()

    async def read(home, **kwargs):
        answer = cloud.read(home)
        if not kwargs.get("reclaim"):
            out.set()
            await deliver.wait()
        return answer

    mock_api.get_scenes.side_effect = read
    return out, deliver


async def test_list_read_answered_before_change_is_dropped(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A poll's list read answered before a change but delivered after it.

    The answer predates the change: it is dropped instead of showing the
    scene off again, and the lists are read again at the next poll (that
    read does not count as the one the change asked for).
    """
    timer = _scene(300004, "O")
    cloud = _Cloud(mock_api, [timer])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    out, deliver = _held_poll_reads(mock_api, cloud)
    coordinator._last_scene_fetch = None  # due at the next poll
    with _no_refresh(config_entry):
        poll = hass.async_create_task(coordinator.async_refresh())
        await out.wait()  # the cloud has answered: off
        await _switch(hass, entity_id, True)
        assert timer["openFlg"] == "1"  # the cloud took the change
        assert hass.states.get(entity_id).state == "on"
        deliver.set()
        await poll
        assert coordinator.last_update_success
        assert hass.states.get(entity_id).state == "on"  # not the older answer
        assert coordinator.homes[HOME].scenes["300004"].record["openFlg"] == "1"
        reads = mock_api.get_scenes.await_count
        await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == reads + 1  # read again
    assert hass.states.get(entity_id).state == "on"
    # Then the slow cadence again.
    await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == reads + 1


async def test_list_read_older_than_action_read_is_dropped(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A poll's read sent before an action's own read, delivered while its change is out.

    The action decided on its newer read (here the scene was renamed in
    the app meanwhile): the older answer does not replace it.
    """
    timer = _scene(300004, "O")
    cloud = _Cloud(mock_api, [timer])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    out, deliver = _held_poll_reads(mock_api, cloud)
    coordinator._last_scene_fetch = None
    poll = hass.async_create_task(coordinator.async_refresh())
    await out.wait()
    cloud.edit(300004, smartName="改名")  # renamed in the app

    async def change(*args):
        deliver.set()
        await poll  # the poll's answer arrives while the change is out
        return cloud.set_open(*args)

    mock_api.set_scene_open.side_effect = change
    with _no_refresh(config_entry):
        await _switch(hass, entity_id, True)
    assert poll.done()
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["friendly_name"].endswith("改名")
    assert coordinator.homes[HOME].scenes["300004"].record["updateTime"] == timer["updateTime"]


async def test_list_read_of_other_home_kept(hass: HomeAssistant, mock_api, config_entry) -> None:
    """An action drops only its own home's answer; every list is read again after it."""
    second = copy.deepcopy(FIXTURE["device"])
    second.update(xkqCode="0000000000000002", homeId=100004, homeName="别墅")
    mock_api.get_wire_controllers.return_value = [copy.deepcopy(FIXTURE["device"]), second]
    mock_api.get_status.side_effect = lambda home, codes: {
        c: copy.deepcopy(FIXTURE["status"]) for c, _ in codes
    }
    mock_api.get_online.return_value = {CODE: True, "0000000000000002": True}
    timer = _scene(300004, "O")
    villa = _Cloud(mock_api, [_scene(300005, "8", homeId=100004)])
    cloud = _Cloud(mock_api, [timer])  # (last: the changes go to this one)
    homes = {HOME: cloud, 100004: villa}
    out, deliver = asyncio.Event(), asyncio.Event()
    hold = False

    async def read(home, **kwargs):
        answer = homes[home].read(home)
        if hold and home == 100004:
            out.set()
            await deliver.wait()
        return answer

    mock_api.get_scenes.side_effect = read
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    switch_id = _entity(hass, SWITCH_DOMAIN, 300004)
    button_id = _entity(hass, BUTTON_DOMAIN, 300005, home=100004)
    villa.edit(300005, smartName="改名")  # renamed in the app
    hold = True
    coordinator._last_scene_fetch = None
    with _no_refresh(config_entry):
        poll = hass.async_create_task(coordinator.async_refresh())
        await out.wait()  # the first home is read, the second one is under way
        await _switch(hass, switch_id, True)
        deliver.set()
        await poll
        assert hass.states.get(switch_id).state == "on"
        # The other home's answer is not older than anything: taken.
        assert hass.states.get(button_id).attributes["friendly_name"].endswith("改名")
        reads = len(mock_api.get_scenes.await_args_list)
        await coordinator.async_refresh()
    assert [c.args[0] for c in mock_api.get_scenes.await_args_list[reads:]] == [HOME, 100004]
    assert hass.states.get(switch_id).state == "on"


async def test_second_change_during_refresh_of_first(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Two scenes switched in a row: the second lands on the read the first's refresh started.

    That read predates the second change: dropped, and the next poll reads
    the list again, although the refresh the second change asks for is
    swallowed by the one under way (Home Assistant's debouncer).
    """
    first, second = _scene(300004, "O"), _scene(300005, "O")
    cloud = _Cloud(mock_api, [first, second])
    await _setup(hass, config_entry)
    first_id = _entity(hass, SWITCH_DOMAIN, 300004)
    second_id = _entity(hass, SWITCH_DOMAIN, 300005)
    out, deliver = _held_poll_reads(mock_api, cloud)
    await _switch(hass, first_id, True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await asyncio.wait_for(out.wait(), 10)  # the refresh after it reads the list
    await _switch(hass, second_id, True)
    assert second["openFlg"] == "1"
    reads = mock_api.get_scenes.await_count
    deliver.set()
    await hass.async_block_till_done()
    assert hass.states.get(first_id).state == "on"
    assert hass.states.get(second_id).state == "on"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2.1))
    await hass.async_block_till_done()
    assert mock_api.get_scenes.await_count == reads + 1  # read again, once
    assert hass.states.get(first_id).state == "on"
    assert hass.states.get(second_id).state == "on"


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
    # A failed change reads the list again too (after the read before it).
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 2


async def test_action_reread_fails(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Never decided on a list that could not be read again: nothing is sent."""
    _serve(mock_api, [_scene(300004, "O")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    mock_api.get_scenes.side_effect = YorkConnectionError("timeout")
    with pytest.raises(HomeAssistantError) as info:
        await _switch(hass, entity_id, True)
    assert info.value.translation_key == "cannot_connect"
    # Taken again although the read logs in for the action: left to the app.
    mock_api.get_scenes.side_effect = YorkSessionTakenError("HTTP 401")
    with pytest.raises(HomeAssistantError) as info:
        await _switch(hass, entity_id, True)
    assert info.value.translation_key == "session_taken"
    assert coordinator.session_yielded
    mock_api.set_scene_open.assert_not_called()
    # The read for an action may take the session back (a user action).
    assert all(call.kwargs == {"reclaim": True} for call in mock_api.get_scenes.await_args_list[1:])


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
    pets = _scene(300013, "H", actionFlg="2", openFlg="0", boxCode=BOX)
    summer = _scene(300014, "B", actionFlg="0", boxCode=BOX)
    _Cloud(mock_api, [pets, summer], ALL_FEATURES)
    await _setup(hass, config_entry)
    pets_id = _entity(hass, SWITCH_DOMAIN, 300013)
    summer_id = _entity(hass, SWITCH_DOMAIN, 300014)
    state = hass.states.get(pets_id)
    assert state.state == "on"
    assert state.attributes["status"] == "hangup"
    with _no_refresh(config_entry):
        await _switch(hass, pets_id, True)  # already running
        mock_api.set_auto_scene.assert_not_called()
        token = pets["updateTime"]
        await _switch(hass, pets_id, False)
        mock_api.set_auto_scene.assert_awaited_once_with(HOME, 300013, "H", "0", token)
        assert hass.states.get(pets_id).state == "off"

        token = summer["updateTime"]
        await _switch(hass, summer_id, True)
        assert mock_api.set_auto_scene.await_args.args == (HOME, 300014, "B", "1", token)
        assert hass.states.get(summer_id).state == "on"
        mock_api.set_auto_scene.side_effect = YorkSceneConflictError("场景冬季防过冷场景")
        with pytest.raises(HomeAssistantError) as info:
            await _switch(hass, summer_id, False)
        assert info.value.translation_key == "scene_conflict"
        assert hass.states.get(summer_id).state == "on"


async def test_linkage_scene_needs_air_box(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Like the app, a linkage or pets scene is not switched on without its air box."""
    records = [
        _scene(300013, "H", actionFlg="0"),  # pets without boxCode
        _scene(300014, "B", actionFlg="0", boxCode=BOX, boxList=[]),
        _scene(300015, "E", actionFlg="0", boxCode=BOX),  # purification needs boxCodes
        _scene(300016, "C", actionFlg="1"),  # running, without boxCode
    ]
    cloud = _Cloud(mock_api, records, ALL_FEATURES)
    await _setup(hass, config_entry)
    for smart_id, name in ((300013, "场景H"), (300014, "场景B"), (300015, "场景E")):
        with pytest.raises(ServiceValidationError) as info:
            await _switch(hass, _entity(hass, SWITCH_DOMAIN, smart_id), True)
        assert info.value.translation_key == "scene_no_sensor"
        assert info.value.translation_placeholders == {"scene": name}
    mock_api.set_auto_scene.assert_not_called()
    with _no_refresh(config_entry):
        # Switching off needs none (runAutoScenes checks from actionFlg "0" only).
        token = records[3]["updateTime"]
        await _switch(hass, _entity(hass, SWITCH_DOMAIN, 300016), False)
        mock_api.set_auto_scene.assert_awaited_once_with(HOME, 300016, "C", "0", token)
        # With its air box added in the app it is switched on.
        cloud.edit(300015, boxCodes=BOX)
        await _switch(hass, _entity(hass, SWITCH_DOMAIN, 300015), True)
    assert mock_api.set_auto_scene.await_args.args[:4] == (HOME, 300015, "E", "1")
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, 300015)).state == "on"


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


async def test_run_replays_current_commands(
    hass: HomeAssistant, mock_api, config_entry, fast_results
) -> None:
    """A scene edited in the app runs its new commands, not the cached ones."""
    cloud = _Cloud(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    edited = copy.deepcopy(XKQ_ENTRY)
    edited["ctrlJson"]["set_temp"] = 20
    cloud.edit(300004, xkqList=[edited])
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    (sent,) = mock_api.run_scene.await_args.args[2]
    assert sent["ctrlJson"]["set_temp"] == 20


@pytest.mark.parametrize(
    ("code", "fields", "flags", "ctrl_type", "scene_id"),
    [
        # On the home page (a favourite, scene2Flg "1"): its buttons' ctrlTypes.
        ("1", {"favoriteFlg": "1"}, None, "YO431", None),
        ("2", {"favoriteFlg": "1"}, None, "YO432", None),
        # Not a favourite, or no scene2Flg: only the scenes tab runs them.
        ("1", {"favoriteFlg": "0"}, None, "YO438", None),
        ("2", {"favoriteFlg": "1"}, {**SCENE_FLAGS, "scene2Flg": "0"}, "YO438", None),
        ("8", {}, None, "YO438", None),
        ("F", {}, ALL_FEATURES, "YO43F", 300004),
    ],
)
async def test_run_ctrl_types(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    fast_results,
    code,
    fields,
    flags,
    ctrl_type,
    scene_id,
) -> None:
    _serve(mock_api, [_scene(300004, code, **fields)], flags)
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


async def test_run_result_errors_keep_polling(
    hass: HomeAssistant, mock_api, config_entry, fast_results, caplog
) -> None:
    """Like the app, a result request that fails is asked again at the next tick."""
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, BUTTON_DOMAIN, 300004)
    # Failing until the timeout: not confirmed (the app's "空调控制失败").
    mock_api.get_control_results.side_effect = YorkConnectionError("timeout")
    await _press(hass, entity_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_control_results.await_count == SCENE_RESULT_POLLS
    warnings = _warnings(caplog)
    assert len(warnings) == 1 and "confirmed" in warnings[0]

    # Errors, then the answer: carried out.
    caplog.clear()
    mock_api.get_control_results.reset_mock(side_effect=True)
    mock_api.get_control_results.side_effect = [
        YorkApiError("multiCtrlResult: HTTP 404"),
        YorkConnectionError("timeout"),
        {"code": "200", "message": "控制成功", "data": []},
    ]
    await _press(hass, entity_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_control_results.await_count == 3
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    "error",
    [
        YorkSessionTakenError("HTTP 401"),
        YorkAuthError("HTTP 403"),
        YorkClientOutdatedError("1.9.0", None),
        YorkRateLimitError(60),
    ],
)
async def test_run_result_stops(
    hass: HomeAssistant, mock_api, config_entry, fast_results, caplog, error
) -> None:
    """Errors of the session, an app update or a rate limit end the check at once."""
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    mock_api.get_control_results.side_effect = error
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_control_results.await_count == 1
    assert not _warnings(caplog)


async def test_run_result_not_read_on_hold(
    hass: HomeAssistant, mock_api, config_entry, fast_results, caplog
) -> None:
    _serve(mock_api, [_scene(300004, "8")])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    # Nothing to follow without tokens.
    mock_api.run_scene.return_value = []
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.get_control_results.assert_not_called()
    # (The status refresh the press scheduled would end the states set below.)
    coordinator._cancel_control_refresh()
    # No result requests while the session is left to the app or polling is paused.
    for attribute in ("_yielded_until", "_outdated_until"):
        with patch.object(coordinator, attribute, 0.0):
            await coordinator._scene_result("x", [{"iezCode": CODE}])
    with patch.object(coordinator, "_paused_until", time.monotonic() + 60):
        await coordinator._scene_result("x", [{"iezCode": CODE}])
    mock_api.get_control_results.assert_not_called()
    assert not _warnings(caplog)


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


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [(403, {}, "HTTP 403"), (200, {"code": "403", "message": "无权限"}, "无权限")],
)
@pytest.mark.parametrize("action", ["switch", "run"])
async def test_scene_action_403_rejected(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    aioclient_mock: AiohttpClientMocker,
    status,
    body,
    message,
    action,
) -> None:
    """A 403 on the change or run (through the real client): rejected, no login, no reauth.

    Reading the list right before worked with the same token, so the
    credentials are fine: the cloud refused this action.
    """
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    records = [_scene(300004, "O"), _scene(300006, "8")]
    detail = {**SCENE_FLAGS, "smartList": records}
    aioclient_mock.get(DETAIL_URL, json={**OK, "data": {"homeDetail": detail}})
    url = (
        f"{API_BASE}/api/apptimer/homes/{HOME}/smarts/300004/openFlg"
        if action == "switch"
        else CTRL_URL
    )
    getattr(aioclient_mock, "put" if action == "switch" else "post")(
        url, side_effect=lambda *a, **k: _resp(body, status)
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    mock_api.get_scenes.side_effect = api.get_scenes
    mock_api.set_scene_open.side_effect = api.set_scene_open
    mock_api.run_scene.side_effect = api.run_scene
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    with pytest.raises(HomeAssistantError) as info:
        if action == "switch":
            await _switch(hass, _entity(hass, SWITCH_DOMAIN, 300004), True)
        else:
            await _press(hass, _entity(hass, BUTTON_DOMAIN, 300006))
    name = "场景O" if action == "switch" else "场景8"
    assert info.value.translation_key == "scene_rejected"
    assert info.value.translation_placeholders == {"scene": name, "message": message}
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not _reauth_started(hass)
    mock_api.relogin.assert_not_called()
    assert api.auth.token == "tok"
    assert "/login" not in {call[1].path for call in aioclient_mock.mock_calls}
    assert not coordinator.session_yielded
    assert coordinator.last_update_success
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, 300004)).state == "off"
    mock_api.get_control_results.assert_not_called()


async def test_run_once_scene(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Greenhouse / party: the cloud runs them; the token is the correctly spelt field."""
    greenhouse = _scene(300010, "G")
    _serve(mock_api, [greenhouse], ALL_FEATURES)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.run_recommended_scene.return_value = {"updateTime": 77, "upadteTime": 78}
    with _no_refresh(config_entry):
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300010))
        mock_api.run_recommended_scene.assert_awaited_once_with(
            HOME, "G", 300010, greenhouse["updateTime"]
        )
        assert coordinator.homes[HOME].scenes["300010"].record["updateTime"] == 77
        # Only the misspelt field: the token of the list read before stays.
        mock_api.run_recommended_scene.return_value = {"upadteTime": 79}
        await _press(hass, _entity(hass, BUTTON_DOMAIN, 300010))
        record = coordinator.homes[HOME].scenes["300010"].record
        assert record["updateTime"] == greenhouse["updateTime"]
    mock_api.run_scene.assert_not_called()


async def test_run_without_devices_refused(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(
        mock_api,
        [_scene(300004, "8", devices=False), _scene(300005, "I", devices=False)],
        ALL_FEATURES,
    )
    await _setup(hass, config_entry)
    for smart_id in (300004, 300005):
        with pytest.raises(ServiceValidationError) as info:
            await _press(hass, _entity(hass, BUTTON_DOMAIN, smart_id))
        assert info.value.translation_key == "scene_no_devices"
    mock_api.run_scene.assert_not_called()
    mock_api.run_recommended_scene.assert_not_called()


async def test_turn_off_scene_without_devices(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A running scene without devices can be switched off (the home page allows it)."""
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


async def test_scene_type_changes(hass: HomeAssistant, mock_api, config_entry, caplog) -> None:
    """A record whose type is unknown (or has another control) leaves the entity unavailable.

    Never an error while the listeners run: on HA 2025.3 one would stop the
    listeners after it on every update.
    """
    records = [_scene(300004, "O", openFlg="1")]
    _serve(mock_api, records)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    switch_id = _entity(hass, SWITCH_DOMAIN, 300004)
    assert hass.states.get(switch_id).state == "on"
    calls = []
    unsub = coordinator.async_add_listener(lambda: calls.append(1))  # after every entity
    for i, code in enumerate((None, "Z", "8"), start=1):
        records[0] = {**records[0], "type": code}
        calls.clear()
        with _later(i * (SCENE_INTERVAL + 1)):
            await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert calls, code
        assert hass.states.get(switch_id).state == "unavailable", code
        with pytest.raises(HomeAssistantError) as info:
            await coordinator.async_set_scene(HOME, "300004", False)
        assert info.value.translation_key == "scene_gone"
    mock_api.set_scene_open.assert_not_called()
    assert "Unexpected error" not in caplog.text
    # Its type back: available again.
    records[0] = {**records[0], "type": "O"}
    with _later(4 * (SCENE_INTERVAL + 1)):
        await coordinator.async_refresh()
    assert hass.states.get(switch_id).state == "on"
    unsub()


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

    def read(home, **kwargs):
        order.append("read")
        return HomeScenes(copy.deepcopy(records), dict(SCENE_FLAGS))

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


async def test_yield_devices_added_in_app(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Devices added in the app while HA left it the session: taken back, read, sent."""
    sleep = copy.deepcopy(SCENES[0])
    cloud = _Cloud(mock_api, [sleep])
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    order = []
    mock_api.relogin.side_effect = lambda *a, **k: order.append("relogin") or mock_api.auth
    mock_api.get_scenes.side_effect = lambda home, **kw: order.append("read") or cloud.read(home)
    mock_api.set_scene_open.side_effect = lambda *args: (
        order.append("send") or cloud.set_open(*args)
    )
    cloud.edit(SLEEP_ID, xkqList=[copy.deepcopy(XKQ_ENTRY)])
    token = sleep["updateTime"]
    with _no_refresh(config_entry):
        await _switch(hass, _entity(hass, SWITCH_DOMAIN, SLEEP_ID), True)
    assert order == ["relogin", "read", "send"]
    mock_api.set_scene_open.assert_awaited_once_with(HOME, SLEEP_ID, "1", token)
    assert not coordinator.session_yielded
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "on"


async def test_yield_switched_off_in_app(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Off in the app during the yield, on in the cache: switching on is sent."""
    timer = _scene(300004, "O", openFlg="1")
    cloud = _Cloud(mock_api, [timer])
    await _setup(hass, config_entry)
    entity_id = _entity(hass, SWITCH_DOMAIN, 300004)
    assert hass.states.get(entity_id).state == "on"
    await _yield(hass, mock_api, config_entry)
    cloud.edit(300004, openFlg="0")
    token = timer["updateTime"]
    with _no_refresh(config_entry):
        await _switch(hass, entity_id, True)
    mock_api.relogin.assert_awaited_once()
    mock_api.set_scene_open.assert_awaited_once_with(HOME, 300004, "1", token)
    assert hass.states.get(entity_id).state == "on"


async def test_yield_run_devices_added(
    hass: HomeAssistant, mock_api, config_entry, fast_results
) -> None:
    """A run button whose scene got devices while HA left the session to the app."""
    cloud = _Cloud(mock_api, [_scene(300004, "8", devices=False)])
    await _setup(hass, config_entry)
    await _yield(hass, mock_api, config_entry)
    cloud.edit(300004, xkqList=[copy.deepcopy(XKQ_ENTRY)])
    await _press(hass, _entity(hass, BUTTON_DOMAIN, 300004))
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.relogin.assert_awaited_once()
    (sent,) = mock_api.run_scene.await_args.args[2]
    assert sent["xkqCode"] == CODE


async def test_yield_kept_for_unknown_scene(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The session is not taken back for a scene HA hides, or one of another kind."""
    flags = dict(SCENE_FLAGS)
    _serve(mock_api, [_scene(300011, "K")], flags)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    flags["sceneKFlg"] = "0"
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    await _yield(hass, mock_api, config_entry)
    reads = mock_api.get_scenes.await_count
    with pytest.raises(ServiceValidationError) as info:
        await coordinator.async_set_scene(HOME, "300011", True)
    assert info.value.translation_key == "scene_hidden"
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_run_scene(HOME, "300011")  # a switch has no run
    assert info.value.translation_key == "scene_gone"
    mock_api.relogin.assert_not_called()
    assert mock_api.get_scenes.await_count == reads
    assert coordinator.session_yielded


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

    mock_api.get_scenes.side_effect = YorkConnectionError("smarts: HTTP 502")
    heat = _heat_status()
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
    mock_api.get_scenes.side_effect = YorkApiError("home detail: code 500 系统异常")
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "off"
    # Retried at the next slow tick, not at every poll.
    calls = mock_api.get_scenes.await_count
    with _later(SCENE_INTERVAL + 3):
        await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == calls


@pytest.mark.parametrize("at_setup", [True, False])
async def test_scene_read_403_keeps_climate(
    hass: HomeAssistant,
    mock_api,
    config_entry,
    aioclient_mock: AiohttpClientMocker,
    caplog,
    at_setup,
) -> None:
    """A 403 on the scene read (through the real client): no login, no reauth."""
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    aioclient_mock.get(DETAIL_URL, side_effect=lambda *a, **k: _resp({}, 403))
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    if at_setup:
        mock_api.get_scenes.side_effect = api.get_scenes
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = api.get_scenes
    heat = _heat_status()
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(heat)}
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert config_entry.state is ConfigEntryState.LOADED
    assert coordinator.last_update_success
    assert hass.states.get(entity_id).state == "heat"
    assert not _reauth_started(hass)
    mock_api.relogin.assert_not_called()
    assert {call[1].path for call in aioclient_mock.mock_calls} == {f"/api/apphome/homes/{HOME}"}
    assert api.auth.token == "tok"
    assert "HTTP 403" in coordinator.scene_error
    assert len(_warnings(caplog)) == 1
    switch = _entity(hass, SWITCH_DOMAIN, SLEEP_ID)
    if at_setup:
        assert switch is None  # never read
    else:
        assert hass.states.get(switch).state == "off"  # the last list is kept


async def test_scene_fetch_other_auth_error_keeps_climate(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Only the session as a whole (taken, conflict) stops the polling, not the scene read."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = YorkAuthError("login: session belongs to a different account")
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert not _reauth_started(hass)


async def test_slow_scene_read_does_not_hold_climate(
    hass: HomeAssistant, mock_api, config_entry, caplog
) -> None:
    """A scene read slower than SCENE_READ_TIMEOUT counts as failed; the poll is published."""
    entity_id = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    async def hang(home, **kwargs):
        await asyncio.sleep(30)

    mock_api.get_scenes.side_effect = hang
    heat = _heat_status()
    mock_api.get_status.side_effect = lambda home, codes: {CODE: dict(heat)}
    coordinator._last_scene_fetch = None  # due now
    start = time.monotonic()
    with patch("custom_components.york_smart_home.coordinator.SCENE_READ_TIMEOUT", 0.05):
        await coordinator.async_refresh()
    assert time.monotonic() - start < 5
    assert coordinator.last_update_success
    assert hass.states.get(entity_id).state == "heat"
    assert coordinator.scene_error == "no answer within 0.05 s"
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, SLEEP_ID)).state == "off"
    assert len(_warnings(caplog)) == 1


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


async def test_scene_fetch_session_conflict(hass: HomeAssistant, mock_api, config_entry) -> None:
    """With "always reclaim", a session revoked again on the scene read asks to log in."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.get_scenes.side_effect = YorkSessionConflictError("revoked again")
    with _later(SCENE_INTERVAL + 1):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert _reauth_started(hass)


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
    """The scene list is read every SCENE_INTERVAL in a status poll, not in every 2 s poll."""
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
    assert mock_api.get_scenes.await_count == reads + 1  # the read before sending
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 2  # and the one after it
    assert mock_api.get_status.await_count == status_calls + 1  # status refresh too
    # Then the slow cadence again.
    await _after_action(hass)
    assert mock_api.get_scenes.await_count == reads + 2


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


async def test_new_home_read_at_once(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A home that appears with a controller has its scenes read at the next poll."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    second = copy.deepcopy(FIXTURE["device"])
    second.update(xkqCode="0000000000000002", homeId=100004, homeName="别墅")
    mock_api.get_wire_controllers.return_value = [copy.deepcopy(FIXTURE["device"]), second]
    mock_api.get_status.side_effect = lambda home, codes: {
        c: copy.deepcopy(FIXTURE["status"]) for c, _ in codes
    }
    mock_api.get_online.return_value = {CODE: True, "0000000000000002": True}
    other = _scene(300004, "8", homeId=100004)
    mock_api.get_scenes.side_effect = lambda home, **kwargs: HomeScenes(
        copy.deepcopy([other] if home == 100004 else SCENES), dict(SCENE_FLAGS)
    )
    coordinator._last_discovery = None  # discovered at the next poll, within SCENE_INTERVAL
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [call.args[0] for call in mock_api.get_scenes.await_args_list] == [
        HOME,
        HOME,
        100004,
    ]
    button_id = _entity(hass, BUTTON_DOMAIN, 300004, home=100004)
    assert hass.states.get(button_id).state == "unknown"
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(button_id).device_id)
    assert device.name == "别墅 York Smart Home"
    # Then back to the slow cadence.
    await coordinator.async_refresh()
    assert mock_api.get_scenes.await_count == 3


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

    def scenes(home, **kwargs):
        if home == 100004:
            return HomeScenes([copy.deepcopy(other)], dict(SCENE_FLAGS))
        raise YorkApiError("home detail: code 500")

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
    assert mock_api.get_scenes.await_args.args == (100004,)  # read again before running


async def test_scene_diagnostics(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(
        mock_api,
        [copy.deepcopy(SCENES[0]), _scene(300004, "8"), _scene(300005, "G")],
    )
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["scene_read_failed"] is False
    (home,) = diag["homes"]
    assert home["home"] == 1 and home["scenes_loaded"] and home["present"]
    assert home["flags"] == SCENE_FLAGS
    sleep, manual, greenhouse = home["scenes"]
    assert sleep == {
        "type": "5",
        "entity": "switch",
        "present": True,
        "offered": True,
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
    # The greenhouse feature is off for the account's home: hidden like the app.
    assert greenhouse["offered"] is False
    assert _entity(hass, BUTTON_DOMAIN, 300005) is None
    text = repr(diag)
    for secret in (str(SLEEP_ID), "300004", "睡眠", "场景8", str(HOME), CODE, str(UPDATE_TIME)):
        assert secret not in text, secret
