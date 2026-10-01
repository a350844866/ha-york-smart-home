"""Firmware updates (OTA): rules, request, the update entity, slow timer, errors, session."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import json
import logging
from unittest.mock import PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.york_smart_home.api import (
    Session,
    YorkApiError,
    YorkAuthError,
    YorkClientOutdatedError,
    YorkConnectionError,
    YorkRateLimitError,
    YorkSessionConflictError,
    YorkSessionTakenError,
)
from custom_components.york_smart_home.const import (
    API_BASE,
    CLIENT_OUTDATED_PAUSE,
    DOMAIN,
    FAILURES_BEFORE_UNAVAILABLE,
    OTA_FIRST_DELAY,
    OTA_INTERVAL,
    OTA_RECHECK_DELAY,
    OTA_RETRY_INTERVAL,
)
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from custom_components.york_smart_home.firmware import (
    SUMMARY_MAX,
    home_controllers,
    installed_version,
    latest_version,
    offered,
    release_summary,
    same_id,
)
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.update import (
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
    SERVICE_SKIP,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotSupported
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import CODE, FIXTURE, OTAS, ota_homes
from .test_api import OK, UNAUTHORIZED, _api, _login_body, _resp, _sequence
from .test_integration import _setup
from .test_session import _issue, _later, _reauth_started, _set_options, _yield

HOME = 100001
HOME2 = 100002
# A second (fake) controller, in a second home.
CODE2 = "0a1b2c3d4e5f6072"
INSTALLED = "C-01603 / W-00258"  # the account's controller record
OTAS_URL = f"{API_BASE}/api/appdevice/getOtas"
LOGIN_URL = f"{API_BASE}/login"

# Updates as the app's bundled sample has them (fake otaIds): the controller
# firmware (MCU), its Wi-Fi module and the indoor units behind it.
MCU_UPDATE = OTAS["offered"]["homeList"][0]["xkqList"][0]["otaList"][0]
WIFI_UPDATE = {
    **MCU_UPDATE,
    "otaId": 400005,
    "otaType": "5",
    "otaDesc": "Wi-Fi 模块优化",
    "popupFlg": "0",
}
IU_UPDATE = {
    **MCU_UPDATE,
    "otaId": 400006,
    "otaType": "2",
    "otaDesc": "室内机程序优化",
    "ouIuList": [{"ouIuId": f"{CODE}0003"}],
}
_TS = FIXTURE["status"]["timestamp"]


def _entry(*updates: dict, ota_flg: object = "0", code: str = CODE) -> dict:
    """A controller's getOtas entry with ``updates`` in its otaList."""
    return {
        "xkqCode": code,
        "otaFlg": ota_flg,
        "statusProgress": None,
        "otaList": [copy.deepcopy(update) for update in updates],
    }


def _serve(mock_api, *entries: dict) -> None:
    """Answer getOtas with ``entries`` for the first requested home."""

    def answer(home_ids):
        homes = ota_homes(home_ids)
        homes[0]["xkqList"] = copy.deepcopy(list(entries))
        return homes

    mock_api.get_otas.side_effect = answer


def _report(seconds: int, **fields) -> dict:
    """The fixture status, ``seconds`` newer, with ``fields``."""
    stamp = _TS + seconds * 1000
    return {**copy.deepcopy(FIXTURE["status"]), "timestamp": stamp, "roundTime": stamp, **fields}


def _reports(mock_api, status: dict) -> None:
    mock_api.get_status.side_effect = lambda home, codes: {
        code: copy.deepcopy(status) for code, _ in codes
    }


def _second_controller(mock_api) -> None:
    """The account gets a second controller, in a second home."""
    second = {**copy.deepcopy(FIXTURE["device"]), "xkqCode": CODE2, "homeId": HOME2}
    mock_api.get_wire_controllers.return_value = [copy.deepcopy(FIXTURE["device"]), second]
    _reports(mock_api, FIXTURE["status"])
    mock_api.get_online.return_value = {CODE: True, CODE2: True}


def _firmware_id(hass: HomeAssistant, code: str = CODE) -> str | None:
    return er.async_get(hass).async_get_entity_id(UPDATE_DOMAIN, DOMAIN, f"{code}_firmware")


def _device(hass: HomeAssistant) -> dr.DeviceEntry:
    """The controller's device (by its firmware entity)."""
    entry = er.async_get(hass).async_get(_firmware_id(hass))
    return dr.async_get(hass).async_get(entry.device_id)


async def _fire(hass: HomeAssistant, seconds: float) -> None:
    """Let ``seconds`` pass for the timers, and finish the reads they start."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def _first_read(hass: HomeAssistant) -> None:
    await _fire(hass, OTA_FIRST_DELAY + 1)


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith("custom_components.york_smart_home")
    ]


# ===================================================================== rules


def test_installed_version() -> None:
    """The OTA page's rows: versionMcu and versionWifi of the controller record."""
    assert installed_version(FIXTURE["device"]) == INSTALLED
    assert installed_version({"versionMcu": "C-01603"}) == "C-01603"
    assert installed_version({"versionWifi": " W-00258 "}) == "W-00258"
    assert installed_version({"versionMcu": "--", "versionWifi": "W-00258"}) == "W-00258"
    assert installed_version({"versionMcu": 1603, "versionWifi": ""}) == "1603"
    for record in ({}, {"versionMcu": None, "versionWifi": "--"}, {"versionMcu": True}):
        assert installed_version(record) is None


def test_offered_like_the_app() -> None:
    """Only a controller whose otaFlg is the string "0"; any type or popupFlg."""
    updates = [MCU_UPDATE, WIFI_UPDATE]  # (the Wi-Fi one has popupFlg "0")
    assert offered(_entry(*updates)) == updates
    assert offered({**_entry(*updates), "otaList": [MCU_UPDATE, "bad", None, 3]}) == [MCU_UPDATE]
    for entry in (
        _entry(MCU_UPDATE, ota_flg="1"),  # installing
        _entry(MCU_UPDATE, ota_flg=0),  # the app compares with "0" ===
        _entry(MCU_UPDATE, ota_flg=None),
        {k: v for k, v in _entry(MCU_UPDATE).items() if k != "otaFlg"},
        {**_entry(), "otaList": None},
        {**_entry(), "otaList": {"otaId": 1}},
        {},
        None,
    ):
        assert offered(entry) == []


def test_latest_version() -> None:
    """Nothing offered: the installed version; else the offered updates by name."""
    assert latest_version(INSTALLED, []) == INSTALLED
    assert latest_version(None, []) is None
    assert latest_version(INSTALLED, [MCU_UPDATE]) == "MCU OTA 400004"
    # The OTA page's row order (MCU, Wi-Fi, indoor units), then other types.
    other = {**MCU_UPDATE, "otaId": "400009", "otaType": "7"}
    assert (
        latest_version(INSTALLED, [other, IU_UPDATE, WIFI_UPDATE, MCU_UPDATE])
        == "MCU OTA 400004, Wi-Fi OTA 400005, IU OTA 400006, type 7 OTA 400009"
    )
    # Several updates of one type, repeated ids, a number otaType, no otaId.
    second = {**MCU_UPDATE, "otaId": 400007, "otaType": 1}
    assert latest_version(None, [MCU_UPDATE, second, MCU_UPDATE]) == "MCU OTA 400004+400007"
    assert latest_version(INSTALLED, [{"otaType": "5"}]) == "Wi-Fi OTA"
    assert latest_version(INSTALLED, [{"otaId": 5}]) == "type ? OTA 5"
    # Never an installed version.
    assert latest_version("MCU", [{"otaType": "1"}]) != "MCU"


def test_latest_version_any_order() -> None:
    """The same updates in another order (or as strings) give the same name.

    Home Assistant clears a skipped version once the latest version
    differs, so a reordered answer must not show a skipped update again.
    """
    updates = [
        {**MCU_UPDATE, "otaId": 400007},
        WIFI_UPDATE,
        {**MCU_UPDATE, "otaId": "400004"},
        {**MCU_UPDATE, "otaId": 99},
        {**MCU_UPDATE, "otaId": "beta"},
        MCU_UPDATE,
    ]
    name = "MCU OTA 99+400004+400007+beta, Wi-Fi OTA 400005"  # ids by value, then texts
    assert latest_version(INSTALLED, updates) == name
    assert latest_version(INSTALLED, list(reversed(updates))) == name
    assert latest_version(INSTALLED, updates[2:] + updates[:2]) == name


def test_release_summary() -> None:
    assert release_summary([]) is None
    assert release_summary(offered(None)) is None
    assert release_summary([MCU_UPDATE]) == "1. 优化显示\n2. 修复已知问题"
    assert release_summary([{**MCU_UPDATE, "otaDesc": 1012}]) == "1012"
    assert release_summary([{**MCU_UPDATE, "otaDesc": "  "}]) is None
    # Per type the first text, named when more than one type is offered.
    empty = {**WIFI_UPDATE, "otaDesc": None}
    assert (
        release_summary([IU_UPDATE, empty, WIFI_UPDATE, MCU_UPDATE])
        == "MCU: 1. 优化显示\n2. 修复已知问题\nWi-Fi: Wi-Fi 模块优化\nIU: 室内机程序优化"
    )
    # Cut to Home Assistant's 255 characters.
    assert release_summary([{**MCU_UPDATE, "otaDesc": "x" * SUMMARY_MAX}]) == "x" * SUMMARY_MAX
    long = release_summary([{**MCU_UPDATE, "otaDesc": "长" * 300}])
    assert len(long) == SUMMARY_MAX
    assert long.endswith("…")


def test_same_id() -> None:
    """The home page's homeId == currentHomeId."""
    assert same_id(100001, "100001")
    assert same_id("100001", 100001.0)
    assert not same_id(100001, 100002)
    assert not same_id(None, None)
    assert not same_id(True, 1)
    assert not same_id("a", "b")


def test_home_controllers() -> None:
    entry = _entry(MCU_UPDATE)
    homes = ["bad", {"homeId": "100002", "xkqList": []}, {"homeId": "100001", "xkqList": [entry]}]
    assert home_controllers(homes, HOME) == {CODE: entry}
    assert home_controllers(homes, HOME2) == {}
    # A home missing from the answer has nothing offered, like in the app.
    assert home_controllers([], HOME) == {}
    assert home_controllers([{"homeId": HOME, "xkqList": None}], HOME) == {}
    # Entries without a code are skipped; a malformed list is unusable.
    junk = [{"otaFlg": "0"}, {"xkqCode": ""}, "bad", None]
    assert home_controllers([{"homeId": HOME, "xkqList": junk}], HOME) == {}
    assert home_controllers([{"homeId": HOME, "xkqList": "bad"}], HOME) is None
    assert home_controllers([{"homeId": HOME, "xkqList": {CODE: entry}}], HOME) is None
    # Asked for together with other homes (the app never does), a home the
    # answer leaves out may just not have been answered: unknown.
    assert home_controllers(homes, HOME, only_home=False) == {CODE: entry}
    assert home_controllers(homes, HOME2, only_home=False) == {}
    assert home_controllers([], HOME, only_home=False) is None
    assert home_controllers(homes, 100003, only_home=False) is None


# ================================================================= request


async def test_get_otas_request(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """OtaService.requestOtas: POST {"homeIdList": [...]} -> data.homeList."""
    homes = copy.deepcopy(OTAS["offered"]["homeList"])
    aioclient_mock.post(OTAS_URL, json={**OK, "data": {"homeList": [*homes, "bad"]}})
    api = _api(hass, auth=Session("tok", 200002))
    assert await api.get_otas([HOME, HOME2]) == homes
    (call,) = aioclient_mock.mock_calls
    assert call[0] == "POST"
    assert str(call[1]) == OTAS_URL
    assert call[2] == {"homeIdList": [HOME, HOME2]}


async def test_get_otas_account_answer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(OTAS_URL, json={**OK, "data": copy.deepcopy(OTAS["account"])})
    api = _api(hass, auth=Session("tok", 200002))
    homes = await api.get_otas([HOME])
    assert homes == [{"homeId": HOME, "xkqList": [], "deviceList": []}]
    assert home_controllers(homes, HOME) == {}


@pytest.mark.parametrize(
    "answer",
    [
        {**OK, "data": None},
        {**OK, "data": {}},
        {**OK, "data": {"homeList": None}},
        {**OK, "data": {"homeList": {"homeId": HOME}}},
        {"code": "500", "message": "系统异常"},
    ],
)
async def test_get_otas_bad_answers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, answer
) -> None:
    aioclient_mock.post(OTAS_URL, json=answer)
    api = _api(hass, auth=Session("tok", 200002))
    with pytest.raises(YorkApiError):
        await api.get_otas([HOME])


@pytest.mark.parametrize(("status", "body"), [(403, {}), (200, {"code": "403", "message": "x"})])
async def test_get_otas_403_is_not_a_login(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body
) -> None:
    """A background read: a 403 fails the read and never logs in again."""
    aioclient_mock.post(OTAS_URL, side_effect=lambda *a, **k: _resp(body, status))
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    with pytest.raises(YorkApiError, match="403") as info:
        await api.get_otas([HOME])
    assert not isinstance(info.value, YorkAuthError)
    assert aioclient_mock.call_count == 1
    assert api.auth.token == "tok"


async def test_get_otas_401_leaves_session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(OTAS_URL, json=UNAUTHORIZED)
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002))
    with pytest.raises(YorkSessionTakenError):
        await api.get_otas([HOME])
    assert aioclient_mock.call_count == 1  # no login


async def test_get_otas_401_with_always_reclaim(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With "always reclaim" on, a 401 logs in again, as for every request."""
    aioclient_mock.post(
        OTAS_URL,
        side_effect=_sequence(UNAUTHORIZED, {**OK, "data": copy.deepcopy(OTAS["account"])}),
    )
    aioclient_mock.get(LOGIN_URL, json=_login_body("fresh"))
    api = _api(hass, password="pw", auth=Session("tok", 200002), reclaim_session=True)
    assert await api.get_otas([HOME]) == [{"homeId": HOME, "xkqList": [], "deviceList": []}]
    assert api.auth.token == "fresh"
    path = "/api/appdevice/getOtas"
    assert [call[1].path for call in aioclient_mock.mock_calls] == [path, "/login", path]


# =================================================================== entity


async def test_account_up_to_date(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The account's answer: its home with no controller entry, nothing offered."""
    await _setup(hass, config_entry)
    mock_api.get_otas.assert_not_awaited()  # not part of setup
    entity_id = _firmware_id(hass)
    state = hass.states.get(entity_id)
    assert state.state == "unknown"  # not read yet
    assert state.attributes["installed_version"] == INSTALLED
    assert state.attributes["latest_version"] is None

    await _first_read(hass)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["installed_version"] == INSTALLED
    assert state.attributes["latest_version"] == INSTALLED
    assert state.attributes["in_progress"] is False
    assert state.attributes["release_summary"] is None
    assert state.attributes["supported_features"] == UpdateEntityFeature.PROGRESS
    assert state.attributes["device_class"] == "firmware"
    assert state.name.endswith("Firmware")
    entry = er.async_get(hass).async_get(entity_id)
    assert entry.entity_category is EntityCategory.CONFIG
    assert entry.translation_key == "firmware"
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device.identifiers == {(DOMAIN, CODE)}


async def test_name_translated(hass: HomeAssistant, mock_api, config_entry) -> None:
    hass.config.language = "zh-Hans"
    await _setup(hass, config_entry)
    assert hass.states.get(_firmware_id(hass)).name.endswith("固件")


async def test_one_entity_per_controller(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The firmware entity is the controller's only new entity."""
    await _setup(hass, config_entry)
    controller = [
        entry
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN and entry.unique_id.startswith(CODE)
    ]
    assert [e.unique_id for e in controller if e.domain == UPDATE_DOMAIN] == [f"{CODE}_firmware"]
    # The account's 18 enabled entities and this one (besides the status
    # banners disabled by default).
    assert len([e for e in controller if e.disabled_by is None]) == 19
    firmware = er.async_get(hass).async_get(_firmware_id(hass))
    assert firmware.disabled_by is None


async def test_update_offered(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The bundled sample's controller firmware update, skipped, replaced, installed."""
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    entity_id = _firmware_id(hass)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["installed_version"] == INSTALLED
    assert state.attributes["latest_version"] == "MCU OTA 400004"
    assert state.attributes["release_summary"] == "1. 优化显示\n2. 修复已知问题"
    assert state.attributes["in_progress"] is False

    # Skipped in HA: off until another update is offered.
    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_SKIP, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["skipped_version"] == "MCU OTA 400004"
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    assert hass.states.get(entity_id).state == "off"  # the same update
    _serve(mock_api, _entry({**MCU_UPDATE, "otaId": 400007}))
    await _fire(hass, OTA_INTERVAL + 1)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["latest_version"] == "MCU OTA 400007"
    assert state.attributes["skipped_version"] is None

    # Installed from the app: no longer offered.
    _serve(mock_api)
    await _fire(hass, OTA_INTERVAL + 1)
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["latest_version"] == INSTALLED


async def test_skip_kept_in_any_order(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A skipped update stays skipped when the answer lists it in another order."""
    second = {**MCU_UPDATE, "otaId": 400007}
    _serve(mock_api, _entry(second, MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    entity_id = _firmware_id(hass)
    assert hass.states.get(entity_id).attributes["latest_version"] == "MCU OTA 400004+400007"
    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_SKIP, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    _serve(mock_api, _entry(MCU_UPDATE, second))
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["latest_version"] == "MCU OTA 400004+400007"
    assert state.attributes["skipped_version"] == "MCU OTA 400004+400007"


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        # Newer releases: Home Assistant's own path, serving brand/icon.png.
        (
            f"/api/brands/integration/{DOMAIN}/icon.png",
            f"/api/brands/integration/{DOMAIN}/icon.png",
        ),
        # Older ones (2025.3): the brands CDN, which only has a placeholder.
        (f"https://brands.home-assistant.io/_/{DOMAIN}/icon.png", None),
        (None, None),
    ],
)
async def test_entity_picture(
    hass: HomeAssistant, mock_api, config_entry, default, expected
) -> None:
    """UpdateEntity's brand picture: kept where Home Assistant serves it, else none."""
    with patch.object(
        UpdateEntity, "entity_picture", new_callable=PropertyMock, return_value=default
    ):
        await _setup(hass, config_entry)
        state = hass.states.get(_firmware_id(hass))
    assert state.attributes.get("entity_picture") == expected


async def test_entity_picture_of_this_release(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The running Home Assistant: its own brand path, or no picture at all."""
    await _setup(hass, config_entry)
    picture = hass.states.get(_firmware_id(hass)).attributes.get("entity_picture")
    assert picture in (None, f"/api/brands/integration/{DOMAIN}/icon.png")


async def test_several_updates_offered(hass: HomeAssistant, mock_api, config_entry) -> None:
    """MCU, Wi-Fi and indoor unit updates of one controller, any popupFlg."""
    _serve(mock_api, _entry(IU_UPDATE, WIFI_UPDATE, MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "on"
    assert state.attributes["latest_version"] == "MCU OTA 400004, Wi-Fi OTA 400005, IU OTA 400006"
    assert state.attributes["release_summary"] == (
        "MCU: 1. 优化显示\n2. 修复已知问题\nWi-Fi: Wi-Fi 模块优化\nIU: 室内机程序优化"
    )


async def test_install_not_offered(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Updates are installed from the app: HA never sends otaCtrl."""
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    entity_id = _firmware_id(hass)
    calls = len(mock_api.mock_calls)
    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    assert len(mock_api.mock_calls) == calls
    assert hass.states.get(entity_id).attributes["in_progress"] is False


async def test_no_installed_version(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A record without versions: the state is unknown, the offer still shown."""
    record = copy.deepcopy(FIXTURE["device"])
    record["versionMcu"] = "--"
    del record["versionWifi"]
    mock_api.get_wire_controllers.return_value = [record]
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "unknown"
    assert state.attributes["installed_version"] is None
    assert state.attributes["latest_version"] == "MCU OTA 400004"
    assert state.attributes["release_summary"] == "1. 优化显示\n2. 修复已知问题"


async def test_answer_value_types(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Numbers where the sample has strings (and back): read like the app reads them.

    The app compares otaType with === "1" / "5" (a number would match no
    row; HA still names it), otaFlg with === "0" (a number 0 offers
    nothing), popupFlg only for its popup.
    """
    update = {**MCU_UPDATE, "otaId": "400008", "otaType": 5, "popupFlg": 0, "otaDesc": 12}
    _serve(mock_api, _entry(update))
    await _setup(hass, config_entry)
    await _first_read(hass)
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "on"
    assert state.attributes["latest_version"] == "Wi-Fi OTA 400008"
    assert state.attributes["release_summary"] == "12"

    _serve(mock_api, _entry(update, ota_flg=0))
    await _fire(hass, OTA_INTERVAL + 1)
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "off"
    assert state.attributes["latest_version"] == INSTALLED
    assert state.attributes["release_summary"] is None


async def test_offline_unavailable(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Like the controller's other entities: unavailable while it is offline."""
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    mock_api.get_online.return_value = {CODE: False}
    coordinator._last_online = None
    await coordinator.async_refresh()
    assert hass.states.get(_firmware_id(hass)).state == "unavailable"
    mock_api.get_online.return_value = {CODE: True}
    coordinator._last_online = None
    await coordinator.async_refresh()
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "on"
    assert state.attributes["latest_version"] == "MCU OTA 400004"


# ------------------------------------------------------------------ install


async def test_installing(hass: HomeAssistant, mock_api, config_entry) -> None:
    """otaFlg "1" (or 1) in the status: in progress, the update still shown."""
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    entity_id = _firmware_id(hass)
    for seconds, flag in ((1, "1"), (2, 1)):
        _reports(mock_api, _report(seconds, otaFlg=flag))
        await coordinator.async_refresh()
        state = hass.states.get(entity_id)
        assert state.state == "on"  # (shown as installing)
        assert state.attributes["in_progress"] is True
        assert state.attributes["update_percentage"] is None
        assert state.attributes["latest_version"] == "MCU OTA 400004"
    _reports(mock_api, _report(3, otaFlg="0"))
    await coordinator.async_refresh()
    assert hass.states.get(entity_id).attributes["in_progress"] is False


async def test_installing_without_offer(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Installing before HA read anything: in progress, the latest version unknown.

    The read is done, but an installing controller takes nothing from it
    (its entry, otaFlg "1" here, offers nothing in the app either): HA does
    not claim the controller is up to date while it installs something.
    """
    _serve(mock_api, _entry(MCU_UPDATE, ota_flg="1"))
    _reports(mock_api, _report(1, otaFlg="1"))
    await _setup(hass, config_entry)
    await _first_read(hass)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    coordinator = config_entry.runtime_data
    assert coordinator.devices[CODE].firmware is None
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "unknown"
    assert state.attributes["in_progress"] is True
    assert state.attributes["latest_version"] is None
    assert state.attributes["installed_version"] == INSTALLED

    # Finished: read again soon, then known.
    _serve(mock_api)
    _reports(mock_api, _report(2, otaFlg="0"))
    await coordinator.async_refresh()
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 2
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "off"
    assert state.attributes["in_progress"] is False
    assert state.attributes["latest_version"] == INSTALLED


@pytest.mark.parametrize(
    "listed",
    [
        # The account's answer: nothing listed for the controller.
        [],
        # Read while it already installed, before its status said so.
        [_entry(MCU_UPDATE, ota_flg="1")],
    ],
)
async def test_installing_unseen_update(
    hass: HomeAssistant, mock_api, config_entry, listed
) -> None:
    """Installing an update the last read did not offer: unknown, not up to date.

    E.g. the app's popup tapped between two reads. Which update is
    installed is unknown, so the latest version is too (as before the
    first read), while the entity shows the installation.
    """
    _serve(mock_api, *listed)
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    entity_id = _firmware_id(hass)
    assert hass.states.get(entity_id).state == "off"

    _reports(mock_api, _report(1, otaFlg="1"))
    await coordinator.async_refresh()
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert state.attributes["in_progress"] is True
    assert state.attributes["installed_version"] == INSTALLED
    assert state.attributes["latest_version"] is None
    assert state.attributes["release_summary"] is None
    # A read meanwhile changes nothing for it.
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    assert hass.states.get(entity_id).state == "unknown"

    # Done: up to date at once, and read again soon.
    _serve(mock_api)
    _reports(mock_api, _report(2, otaFlg="0"))
    await coordinator.async_refresh()
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["in_progress"] is False
    assert state.attributes["latest_version"] == INSTALLED
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 3
    assert hass.states.get(entity_id).state == "off"


async def test_read_while_installing(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The slow reads go on; only the installing controller keeps its last offer.

    Whatever the answer lists for it meanwhile (here its entry turned to
    otaFlg "1"), it goes on showing the update being installed. The other
    controller takes each answer, also while the first one sits offline in
    "OTA升级中" (a stale otaFlg "1" holds nobody back).
    """
    _second_controller(mock_api)

    def offer(first: list, second: list):
        def answer(home_ids):
            assert home_ids == [HOME, HOME2]
            homes = ota_homes(home_ids)
            homes[0]["xkqList"] = copy.deepcopy(first)
            homes[1]["xkqList"] = copy.deepcopy(second)
            return homes

        mock_api.get_otas.side_effect = answer

    offer([_entry(MCU_UPDATE)], [])
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    first, second = _firmware_id(hass), _firmware_id(hass, CODE2)
    assert hass.states.get(first).attributes["latest_version"] == "MCU OTA 400004"
    assert hass.states.get(second).state == "off"

    # The first controller installs its update; a Wi-Fi update for the second.
    reports = {CODE: _report(1, otaFlg="1"), CODE2: _report(1)}
    mock_api.get_status.side_effect = lambda home, codes: {
        code: copy.deepcopy(reports[code]) for code, _ in codes
    }
    await coordinator.async_refresh()
    offer([_entry(MCU_UPDATE, ota_flg="1")], [_entry(WIFI_UPDATE, code=CODE2)])
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    state = hass.states.get(first)
    assert state.state == "on"
    assert state.attributes["in_progress"] is True
    assert state.attributes["latest_version"] == "MCU OTA 400004"
    assert state.attributes["release_summary"] == "1. 优化显示\n2. 修复已知问题"
    state = hass.states.get(second)
    assert state.state == "on"
    assert state.attributes["in_progress"] is False
    assert state.attributes["latest_version"] == "Wi-Fi OTA 400005"
    assert state.attributes["release_summary"] == "Wi-Fi 模块优化"

    # Offline while still reporting otaFlg "1": unavailable, entry kept; the
    # second controller's update was installed meanwhile.
    mock_api.get_online.return_value = {CODE: False, CODE2: True}
    coordinator._last_online = None
    await coordinator.async_refresh()
    assert hass.states.get(first).state == "unavailable"
    offer([], [])
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 3
    assert hass.states.get(second).state == "off"
    assert coordinator.devices[CODE].firmware == _entry(MCU_UPDATE)  # kept

    # Back online, the installation over: read again soon.
    mock_api.get_online.return_value = {CODE: True, CODE2: True}
    coordinator._last_online = None
    reports[CODE] = _report(2, otaFlg="0")
    await coordinator.async_refresh()
    state = hass.states.get(first)
    assert state.state == "on"  # the last offer, until read again
    assert state.attributes["in_progress"] is False
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 4
    assert hass.states.get(first).state == "off"


async def test_update_finished(hass: HomeAssistant, mock_api, config_entry) -> None:
    """otaFlg back to "0": the offer is read again soon, then the new versions."""
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    entity_id = _firmware_id(hass)
    _reports(mock_api, _report(1, otaFlg="1"))
    await coordinator.async_refresh()

    # Installed: the cloud no longer offers it, the record has the new version.
    _serve(mock_api)
    new_record = {**copy.deepcopy(FIXTURE["device"]), "versionMcu": "C-01700"}
    mock_api.get_wire_controllers.return_value = [new_record]
    discoveries = mock_api.get_wire_controllers.await_count
    _reports(mock_api, _report(2, otaFlg="0"))
    await coordinator.async_refresh()
    state = hass.states.get(entity_id)
    assert state.attributes["in_progress"] is False
    assert state.state == "on"  # until the offer is read again
    await _fire(hass, OTA_RECHECK_DELAY - 10)
    assert mock_api.get_otas.await_count == 1
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 2
    assert hass.states.get(entity_id).state == "off"

    # Then the controller records are read again.
    await coordinator.async_refresh()
    assert mock_api.get_wire_controllers.await_count == discoveries + 1
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["installed_version"] == "C-01700 / W-00258"
    assert state.attributes["latest_version"] == "C-01700 / W-00258"
    device = _device(hass)
    assert (device.sw_version, device.hw_version) == ("C-01700", "W-00258")
    # The new version makes the offer be read once more, soon.
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 3
    await coordinator.async_refresh()
    assert mock_api.get_wire_controllers.await_count == discoveries + 1


async def test_new_version_from_discovery(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Installed while HA saw no otaFlg (e.g. offline): the discovery shows it."""
    _serve(mock_api, _entry(WIFI_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    entity_id = _firmware_id(hass)
    # The same versions: no extra read.
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 1

    _serve(mock_api)
    mock_api.get_wire_controllers.return_value = [
        {**copy.deepcopy(FIXTURE["device"]), "versionWifi": "W-00300"}
    ]
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    state = hass.states.get(entity_id)
    assert state.attributes["installed_version"] == "C-01603 / W-00300"
    assert state.state == "on"  # the last offer, until read again
    device = _device(hass)
    assert (device.sw_version, device.hw_version) == ("C-01603", "W-00300")
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 2
    assert hass.states.get(entity_id).state == "off"


# ------------------------------------------------------------------- answers


@pytest.mark.parametrize(
    ("homes", "expected"),
    [
        # The home missing from the answer, or without a list: nothing offered.
        ([], "off"),
        ([{"homeId": HOME2, "xkqList": [_entry(MCU_UPDATE)]}], "off"),
        ([{"homeId": HOME, "xkqList": None}], "off"),
        # Not offered by the app.
        ([{"homeId": HOME, "xkqList": [_entry(MCU_UPDATE, ota_flg=0)]}], "off"),
        ([{"homeId": HOME, "xkqList": [_entry(MCU_UPDATE, ota_flg="1")]}], "off"),
        ([{"homeId": HOME, "xkqList": [_entry(MCU_UPDATE, code="0000000000000009")]}], "off"),
        ([{"homeId": HOME, "xkqList": [{**_entry(), "otaList": None}]}], "off"),
        ([{"homeId": HOME, "xkqList": [{**_entry(), "otaList": "bad"}]}], "off"),
        # Offered: the home's id as a string, junk around it.
        (
            ["bad", {"homeId": str(HOME), "xkqList": [None, {**_entry(MCU_UPDATE)}]}],
            "on",
        ),
        ([{"homeId": HOME, "xkqList": [_entry(MCU_UPDATE, "bad", None)]}], "on"),
        # Unusable for the home: nothing known yet.
        ([{"homeId": HOME, "xkqList": "bad"}], "unknown"),
    ],
)
async def test_answers(hass: HomeAssistant, mock_api, config_entry, homes, expected) -> None:
    mock_api.get_otas.side_effect = lambda home_ids: copy.deepcopy(homes)
    await _setup(hass, config_entry)
    await _first_read(hass)
    assert hass.states.get(_firmware_id(hass)).state == expected
    assert config_entry.runtime_data.firmware_error is None


async def test_unusable_answer_keeps_last(hass: HomeAssistant, mock_api, config_entry) -> None:
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    mock_api.get_otas.side_effect = lambda home_ids: [{"homeId": HOME, "xkqList": {"a": 1}}]
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    state = hass.states.get(_firmware_id(hass))
    assert state.state == "on"
    assert state.attributes["latest_version"] == "MCU OTA 400004"


async def test_all_homes_in_one_request(hass: HomeAssistant, mock_api, config_entry) -> None:
    """One read for every home, its entries matched by homeId and xkqCode."""
    _second_controller(mock_api)

    def answer(home_ids):
        # The homes in another order, their ids as strings.
        homes = ota_homes(list(reversed(home_ids)))
        for home in homes:
            home["homeId"] = str(home["homeId"])
        homes[0]["xkqList"] = [_entry(MCU_UPDATE, code=CODE2)]
        return homes

    mock_api.get_otas.side_effect = answer
    await _setup(hass, config_entry)
    await _first_read(hass)
    mock_api.get_otas.assert_awaited_once_with([HOME, HOME2])
    assert hass.states.get(_firmware_id(hass)).state == "off"
    assert hass.states.get(_firmware_id(hass, CODE2)).state == "on"


async def test_home_left_out_of_answer(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Several homes asked for, one left out of the answer: not taken as up to date.

    The app never asks for more than one home, so whether the cloud answers
    for all of them is unknown: a home the answer leaves out keeps what was
    read before (unknown before the first read).
    """
    _second_controller(mock_api)
    answered = [HOME]

    def answer(home_ids):
        assert home_ids == [HOME, HOME2]
        homes = ota_homes([home_id for home_id in home_ids if home_id in answered])
        for home in homes:
            if home["homeId"] == HOME2:
                home["xkqList"] = [_entry(MCU_UPDATE, code=CODE2)]
        return homes

    mock_api.get_otas.side_effect = answer
    await _setup(hass, config_entry)
    await _first_read(hass)
    first, second = _firmware_id(hass), _firmware_id(hass, CODE2)
    assert hass.states.get(first).state == "off"
    assert hass.states.get(second).state == "unknown"
    assert config_entry.runtime_data.devices[CODE2].firmware is None
    assert config_entry.runtime_data.firmware_error is None

    answered[:] = [HOME, HOME2]
    await _fire(hass, OTA_INTERVAL + 1)
    state = hass.states.get(second)
    assert state.state == "on"
    assert state.attributes["latest_version"] == "MCU OTA 400004"

    # Left out again: the last offer is kept.
    answered[:] = [HOME]
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 3
    state = hass.states.get(second)
    assert state.state == "on"
    assert state.attributes["latest_version"] == "MCU OTA 400004"
    assert hass.states.get(first).state == "off"


async def test_new_controller_read_soon(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    _second_controller(mock_api)
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_firmware_id(hass, CODE2)).state == "unknown"
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 2
    assert mock_api.get_otas.await_args.args == ([HOME, HOME2],)
    assert hass.states.get(_firmware_id(hass, CODE2)).state == "off"


@pytest.mark.parametrize("change", ["new controller", "new version"])
async def test_change_kept_when_discovery_fails(
    hass: HomeAssistant, mock_api, config_entry, change
) -> None:
    """A change the discovery saw before failing still gets its read soon.

    The next discovery sees no change any more (the controller is known,
    its record updated), so the read must be armed as soon as it is seen.
    """
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    if change == "new controller":
        _second_controller(mock_api)
    else:
        mock_api.get_wire_controllers.return_value = [
            {**copy.deepcopy(FIXTURE["device"]), "versionMcu": "C-01700"}
        ]
    detail = mock_api.get_wire_controller_detail.side_effect
    mock_api.get_wire_controller_detail.side_effect = YorkRateLimitError(60)
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    assert coordinator._paused_until is not None  # the 429 stopped the discovery
    assert coordinator._last_discovery is None
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_RECHECK_DELAY, abs=5)

    # The pause over, the discovery is done again (seeing no change).
    mock_api.get_wire_controller_detail.side_effect = detail
    with _later(61):
        await coordinator.async_refresh()
    assert coordinator._last_discovery is not None
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 2
    if change == "new controller":
        assert mock_api.get_otas.await_args.args == ([HOME, HOME2],)
        assert hass.states.get(_firmware_id(hass, CODE2)).state == "off"
    else:
        state = hass.states.get(_firmware_id(hass))
        assert state.attributes["installed_version"] == "C-01700 / W-00258"
        assert state.state == "off"


# ------------------------------------------------------------ slow timer


async def test_cadence(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Read shortly after setup, then every 6 hours; never in the 2 s polling."""
    await _setup(hass, config_entry)
    status_calls = mock_api.get_status.await_count
    now = dt_util.utcnow()
    for i in range(1, 6):
        async_fire_time_changed(hass, now + timedelta(seconds=2.1 * i))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_status.await_count - status_calls == 5  # the polling runs
    mock_api.get_otas.assert_not_awaited()
    await _fire(hass, OTA_FIRST_DELAY - 5)
    mock_api.get_otas.assert_not_awaited()
    await _fire(hass, OTA_FIRST_DELAY + 1)
    assert mock_api.get_otas.await_count == 1  # the timer fires
    await _fire(hass, OTA_INTERVAL - 60)
    assert mock_api.get_otas.await_count == 1
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 3
    coordinator = config_entry.runtime_data
    assert coordinator.last_update_success
    # Each read arms the next one, OTA_INTERVAL later.
    assert coordinator._unsub_firmware_timer is not None
    assert coordinator._firmware_due == pytest.approx(
        hass.loop.time() + OTA_INTERVAL, abs=OTA_INTERVAL / 100
    )
    # The status polling goes on with its own timer (the firmware timer
    # never replaces it).
    status_calls = mock_api.get_status.await_count
    now = dt_util.utcnow()
    for i in range(1, 4):
        async_fire_time_changed(hass, now + timedelta(seconds=3.1 * i))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.get_status.await_count - status_calls >= 3
    assert mock_api.get_otas.await_count == 3


async def test_read_does_not_hold_polling(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A read that hangs: setup is done and the climate polling goes on."""
    answered = asyncio.Event()

    async def slow(home_ids):
        await answered.wait()
        return ota_homes(home_ids)

    mock_api.get_otas.side_effect = slow
    await _setup(hass, config_entry)
    climate = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)
    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(seconds=OTA_FIRST_DELAY + 1))
    await hass.async_block_till_done()
    mock_api.get_otas.assert_awaited_once()
    status_calls = mock_api.get_status.await_count
    _reports(mock_api, _report(1, wire_controller_makecold="0", wire_controller_heating="1"))
    for i in range(1, 4):
        async_fire_time_changed(hass, now + timedelta(seconds=OTA_FIRST_DELAY + 1 + 2.1 * i))
        await hass.async_block_till_done()
    assert mock_api.get_status.await_count - status_calls == 3
    assert hass.states.get(climate).state == "heat"
    assert hass.states.get(_firmware_id(hass)).state == "unknown"
    answered.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(_firmware_id(hass)).state == "off"


@pytest.mark.parametrize("trigger", ["installed", "new controller"])
async def test_recheck_asked_during_read(
    hass: HomeAssistant, mock_api, config_entry, trigger
) -> None:
    """A reason to read again soon while a read is under way: soon after it.

    The answer under way may predate the change (e.g. still offer an
    update installed meanwhile, and an indoor unit update changes no
    version the discovery would see), so the next read comes
    OTA_RECHECK_DELAY after it rather than OTA_INTERVAL.
    """
    answered = asyncio.Event()

    async def slow(home_ids):
        await answered.wait()
        return ota_homes(home_ids)

    mock_api.get_otas.side_effect = slow
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=OTA_FIRST_DELAY + 1))
    await hass.async_block_till_done()
    mock_api.get_otas.assert_awaited_once_with([HOME])
    assert coordinator._firmware_reading

    if trigger == "installed":
        for seconds, flag in ((1, "1"), (2, "0")):
            _reports(mock_api, _report(seconds, otaFlg=flag))
            await coordinator.async_refresh()
    else:
        _second_controller(mock_api)
        coordinator._last_discovery = None
        await coordinator.async_refresh()
    assert coordinator._firmware_due is None  # nothing armed during the read

    answered.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator._firmware_reading
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_RECHECK_DELAY, abs=5)
    await _fire(hass, OTA_RECHECK_DELAY - 10)
    assert mock_api.get_otas.await_count == 1
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    assert mock_api.get_otas.await_count == 2
    if trigger == "new controller":
        assert mock_api.get_otas.await_args.args == ([HOME, HOME2],)
        assert hass.states.get(_firmware_id(hass, CODE2)).state == "off"
    # Then the slow cadence again.
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_INTERVAL, abs=5)


async def test_recheck_never_postpones(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A read due sooner than OTA_RECHECK_DELAY is not put off by a new reason."""
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    coordinator._arm_firmware_timer(30)  # e.g. a retry, due in 30 s
    due = coordinator._firmware_due
    _second_controller(mock_api)
    coordinator._last_discovery = None
    await coordinator.async_refresh()
    assert CODE2 in coordinator.devices
    assert coordinator._firmware_due == due
    await _fire(hass, 31)
    assert mock_api.get_otas.await_count == 2
    assert mock_api.get_otas.await_args.args == ([HOME, HOME2],)


async def test_no_controller_yet(hass: HomeAssistant, mock_api, config_entry, caplog) -> None:
    """Set up while the app holds the session (no devices): read once one is found."""
    caplog.set_level(logging.DEBUG, logger="custom_components.york_smart_home")
    mock_api.get_wire_controllers.side_effect = YorkSessionTakenError("HTTP 401")
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.devices == {}
    assert coordinator.session_yielded
    await _first_read(hass)
    mock_api.get_otas.assert_not_awaited()
    assert "York firmware check put off: no wire controller (yet)" in caplog.text
    assert coordinator.firmware_error is None
    # Looked at again after OTA_RETRY_INTERVAL.
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_RETRY_INTERVAL, abs=5)

    # The session taken back well before that (here: "always reclaim"
    # switched on): the controller is discovered, and read a minute later.
    mock_api.get_wire_controllers.side_effect = None
    await _set_options(hass, config_entry, reclaim_session=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.session_yielded
    mock_api.relogin.assert_awaited_once()
    entity_id = _firmware_id(hass)
    assert hass.states.get(entity_id).state == "unknown"
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_RECHECK_DELAY, abs=5)
    await _fire(hass, OTA_RECHECK_DELAY - 10)
    mock_api.get_otas.assert_not_awaited()
    await _fire(hass, OTA_RECHECK_DELAY + 1)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    assert hass.states.get(entity_id).state == "off"


async def test_no_read_while_polls_fail(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Polls failing (the entities unavailable): no read until they work again."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkConnectionError("HTTP 502")
    for _ in range(FAILURES_BEFORE_UNAVAILABLE):
        await coordinator.async_refresh()
    assert not coordinator.last_update_success
    await _first_read(hass)
    mock_api.get_otas.assert_not_awaited()
    assert coordinator.firmware_error is None
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_RETRY_INTERVAL, abs=5)

    mock_api.get_status.side_effect = good
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get(_firmware_id(hass)).state == "unknown"
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    assert hass.states.get(_firmware_id(hass)).state == "off"


async def test_fetch_failure_isolated(hass: HomeAssistant, mock_api, config_entry, caplog) -> None:
    """A failed read keeps the last information; polling is not affected."""
    caplog.set_level(logging.DEBUG, logger="custom_components.york_smart_home")
    _serve(mock_api, _entry(MCU_UPDATE))
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    climate = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, CODE)
    entity_id = _firmware_id(hass)

    mock_api.get_otas.side_effect = YorkConnectionError("/api/appdevice/getOtas: HTTP 502")
    _reports(mock_api, _report(1, wire_controller_makecold="0", wire_controller_heating="1"))
    await _fire(hass, OTA_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    assert coordinator.last_update_success
    assert hass.states.get(climate).state == "heat"  # the polling went on
    state = hass.states.get(entity_id)
    assert state.state == "on"  # the last information is kept
    assert state.attributes["latest_version"] == "MCU OTA 400004"
    assert coordinator.firmware_error == "/api/appdevice/getOtas: HTTP 502"
    assert [w for w in _warnings(caplog) if "firmware" in w] == [
        "Could not read the York firmware information (/api/appdevice/getOtas: HTTP 502); "
        f"keeping the last one and trying again in {OTA_RETRY_INTERVAL // 60} minutes"
    ]
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["firmware_read_failed"] is True

    # Retried after OTA_RETRY_INTERVAL, warned only once.
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 3
    assert len([w for w in _warnings(caplog) if "firmware" in w]) == 1
    _serve(mock_api)
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 4
    assert coordinator.firmware_error is None
    assert "York firmware information can be read again" in caplog.text
    assert hass.states.get(entity_id).state == "off"


@pytest.mark.parametrize(
    "error",
    [
        YorkSessionTakenError("/api/appdevice/getOtas: HTTP 401"),
        YorkSessionConflictError("taken again"),
        YorkAuthError("expired"),
        YorkRateLimitError(120),
        YorkClientOutdatedError("1.9.0", "https://example.invalid/app"),
    ],
)
async def test_session_errors_left_to_polling(
    hass: HomeAssistant, mock_api, config_entry, caplog, error
) -> None:
    """Errors of the session: the status polling handles them, not the read."""
    mock_api.get_otas.side_effect = error
    await _setup(hass, config_entry)
    await _first_read(hass)
    coordinator = config_entry.runtime_data
    mock_api.get_otas.assert_awaited_once()
    assert not coordinator.session_yielded
    assert not coordinator.client_outdated
    assert coordinator._paused_until is None
    assert _issue(hass, config_entry, "session_taken") is None
    assert _issue(hass, config_entry, "client_outdated") is None
    assert not _reauth_started(hass)
    assert not [w for w in _warnings(caplog) if "firmware" in w]
    assert coordinator.firmware_error is None
    assert coordinator.last_update_success
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2


async def test_unexpected_error_keeps_timer(
    hass: HomeAssistant, mock_api, config_entry, caplog
) -> None:
    mock_api.get_otas.side_effect = KeyError("homeList")
    await _setup(hass, config_entry)
    await _first_read(hass)
    assert "Unexpected error reading the York firmware information" in caplog.text
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    assert mock_api.get_otas.await_count == 2
    assert config_entry.runtime_data.last_update_success


async def test_no_requests_while_yielded(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Nothing is read while the session is left to the app; read after it."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(hass, mock_api, config_entry)
    await _first_read(hass)
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_not_awaited()
    assert coordinator.session_yielded
    assert mock_api.relogin.await_count == 0  # never taken back for it
    with _later(31 * 60):
        await coordinator.async_refresh()
    assert not coordinator.session_yielded
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    assert hass.states.get(_firmware_id(hass)).state == "off"


async def test_no_requests_while_paused(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Rate limited or an app update demanded: no read either; read once it is over."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkRateLimitError(3600)
    await coordinator.async_refresh()
    await _first_read(hass)
    mock_api.get_otas.assert_not_awaited()
    coordinator._paused_until = None
    mock_api.get_status.side_effect = YorkClientOutdatedError("1.9.0", None)
    await coordinator.async_refresh()
    assert coordinator.client_outdated
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_not_awaited()

    # The hourly pause over, the cloud answers again: read at the next try.
    mock_api.get_status.side_effect = good
    with _later(CLIENT_OUTDATED_PAUSE + 1):
        await coordinator.async_refresh()
    assert not coordinator.client_outdated
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    assert hass.states.get(_firmware_id(hass)).state == "off"


async def test_no_reads_with_polling_disabled(
    hass: HomeAssistant, mock_api, config_entry, caplog
) -> None:
    """The entry's "Enable polling for changes" off: no firmware reads either.

    Home Assistant then stops the status polling (DataUpdateCoordinator);
    the firmware timer sends nothing too. Switching the option back on
    (which reloads the entry in the UI) lets the next check read.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.york_smart_home")
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, pref_disable_polling=True)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data
    assert coordinator.last_update_success  # the first refresh at setup
    status_calls = mock_api.get_status.await_count
    await _first_read(hass)
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_not_awaited()
    assert mock_api.get_status.await_count == status_calls  # no timed polls either
    assert "York firmware check put off: polling is disabled for the entry" in caplog.text
    assert coordinator.firmware_error is None
    assert coordinator._firmware_due == pytest.approx(hass.loop.time() + OTA_RETRY_INTERVAL, abs=5)
    assert hass.states.get(_firmware_id(hass)).state == "unknown"
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["polling_disabled"] is True

    hass.config_entries.async_update_entry(config_entry, pref_disable_polling=False)
    await _fire(hass, OTA_RETRY_INTERVAL + 1)
    mock_api.get_otas.assert_awaited_once_with([HOME])
    assert hass.states.get(_firmware_id(hass)).state == "off"
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["polling_disabled"] is False


async def test_unload_stops_checks(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await _fire(hass, OTA_FIRST_DELAY + 1)
    mock_api.get_otas.assert_not_awaited()


async def test_unload_during_read(hass: HomeAssistant, mock_api, config_entry) -> None:
    """A read cut short by the unload arms no new timer."""
    answered = asyncio.Event()

    async def slow(home_ids):
        await answered.wait()
        return ota_homes(home_ids)

    mock_api.get_otas.side_effect = slow
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=OTA_FIRST_DELAY + 1))
    await hass.async_block_till_done()
    mock_api.get_otas.assert_awaited_once()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator._unsub_firmware_timer is None
    answered.set()
    await _fire(hass, OTA_INTERVAL + 1)
    mock_api.get_otas.assert_awaited_once()


# ------------------------------------------------------------- diagnostics


async def test_diagnostics(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The last answer's allowlisted fields: no codes, otaIds or unit ids."""
    _serve(mock_api, _entry(MCU_UPDATE, IU_UPDATE))
    await _setup(hass, config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["devices"][0]["firmware"] is None  # not read yet
    assert diagnostics["firmware_read_failed"] is False

    await _first_read(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    allowed = {k: v for k, v in MCU_UPDATE.items() if k != "otaId"}
    assert diagnostics["devices"][0]["firmware"] == {
        "listed": True,
        "otaFlg": "0",
        "statusProgress": None,
        "otaList": [
            allowed,
            {**allowed, "otaType": "2", "otaDesc": "室内机程序优化", "indoor_unit_count": 1},
        ],
    }
    assert CODE not in json.dumps(diagnostics, ensure_ascii=False)
    firmware = json.dumps(diagnostics["devices"][0]["firmware"], ensure_ascii=False)
    assert "400004" not in firmware
    assert "400006" not in firmware
    assert "ouIuId" not in firmware


async def test_diagnostics_unknown_shapes(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Fields the app never reads: plain values only, never the controller code.

    statusProgress is null in the only sample; were it a structure (say
    the progress per indoor unit, whose ids start with the controller's
    code), only its type and size are exported.
    """
    update = {
        **IU_UPDATE,
        "confirmTime": {"by": CODE},
        "otaProcess": [f"{CODE}0003", 50],
        "confirmFlg": 1,
        "romVersionD": f"{CODE}0003-R2",
    }
    entry = _entry(update)
    entry["statusProgress"] = {"ouIuList": [{"ouIuId": f"{CODE}0003", "progress": 50}]}
    _serve(mock_api, entry)
    await _setup(hass, config_entry)
    await _first_read(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    firmware = diagnostics["devices"][0]["firmware"]
    assert firmware["statusProgress"] == "<dict of 1>"
    assert firmware["otaList"] == [
        {
            "otaType": "2",
            "otaDesc": "室内机程序优化",
            "confirmFlg": 1,
            "confirmTime": "<dict of 1>",
            "otaProcess": "<list of 2>",
            "popupFlg": "1",
            "romVersionD": "<xkqCode>0003-R2",
            "indoor_unit_count": 1,
        }
    ]
    assert CODE not in json.dumps(diagnostics, ensure_ascii=False)

    # Plain values as they are.
    for progress in ("50", 50, 12.5, True):
        entry["statusProgress"] = progress
        _serve(mock_api, entry)
        await _fire(hass, OTA_INTERVAL + 1)
        diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
        assert diagnostics["devices"][0]["firmware"]["statusProgress"] == progress


async def test_diagnostics_account(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    await _first_read(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["devices"][0]["firmware"] == {"listed": False, "otaList": []}
    assert diagnostics["devices"][0]["info"]["versionMcu"] == "C-01603"
