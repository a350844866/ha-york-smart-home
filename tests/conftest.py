"""Fixtures for York Smart Home tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.york_smart_home.api import HomeScenes, Session
from custom_components.york_smart_home.const import (
    CONF_CLIENT_ID,
    CONF_PHONE,
    CONF_SESSION,
    DOMAIN,
)
from homeassistant.const import CONF_PASSWORD

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "device.json").read_text("utf-8"))
CODE = FIXTURE["device"]["xkqCode"]
PHONE = "13800000000"
# The account's home detail as the scene read keeps it: one sleep curve
# scene, off and without devices, and the home's feature flags.
HOME_DETAIL = json.loads((Path(__file__).parent / "fixtures" / "scenes.json").read_text("utf-8"))[
    "homeDetail"
]
SCENES = HOME_DETAIL["smartList"]
SCENE_FLAGS = {key: value for key, value in HOME_DETAIL.items() if key != "smartList"}
# getOtas answers (data): the account's (no update offered) and one offering
# a controller firmware update, shaped like the app's bundled sample.
OTAS = json.loads((Path(__file__).parent / "fixtures" / "otas.json").read_text("utf-8"))


def ota_homes(home_ids: list) -> list[dict]:
    """The account's answer (data.homeList): its home entry for each requested home."""
    home = OTAS["account"]["homeList"][0]
    return [{**copy.deepcopy(home), "homeId": home_id} for home_id in home_ids]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_delays():
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 0):
        yield


@pytest.fixture
def mock_api():
    """Patch YorkApi everywhere with a fake backed by the HAR capture."""
    with (
        patch("custom_components.york_smart_home.YorkApi", autospec=True) as cls,
        patch("custom_components.york_smart_home.config_flow.YorkApi", new=cls),
    ):

        def construct(session, phone, **kwargs):
            api.client_id = kwargs.get("client_id") or "cid"
            api.ctor_kwargs = kwargs
            return api

        api = cls.return_value
        cls.side_effect = construct
        api.auth = Session(token="tok", user_id=200002, refresh_token="rt")

        async def login(*args, **kwargs):
            return api.auth

        api.login_password = AsyncMock(side_effect=login)
        api.relogin = AsyncMock(side_effect=login)
        api.login_sms = AsyncMock(side_effect=login)
        api.request_sms_code = AsyncMock()
        api.get_wire_controllers = AsyncMock(return_value=[copy.deepcopy(FIXTURE["device"])])
        api.get_wire_controller_detail = AsyncMock(
            side_effect=lambda home, code: copy.deepcopy(FIXTURE["device"]["constantJson"])
        )
        api.get_status = AsyncMock(
            side_effect=lambda home, codes: {CODE: copy.deepcopy(FIXTURE["status"])}
        )
        api.get_online = AsyncMock(return_value={CODE: True})
        api.control = AsyncMock(return_value=None)
        api.set_simple_timer = AsyncMock(return_value={})
        api.cancel_simple_timer = AsyncMock(return_value={})
        api.get_scenes = AsyncMock(
            side_effect=lambda home, **kwargs: HomeScenes(
                copy.deepcopy(SCENES), copy.deepcopy(SCENE_FLAGS)
            )
        )
        api.run_scene = AsyncMock(return_value=[{"iezCode": CODE, "hiiotToken": "h1"}])
        api.get_control_results = AsyncMock(
            return_value={"code": "200", "message": "控制成功", "data": []}
        )
        api.set_scene_open = AsyncMock(return_value={"updateTime": 1700000000009})
        api.run_recommended_scene = AsyncMock(return_value={"updateTime": 1700000000009})
        api.set_auto_scene = AsyncMock(return_value={"updateTime": 1700000000009})
        api.get_otas = AsyncMock(side_effect=lambda home_ids: ota_homes(home_ids))
        api.unsupported_count = 0
        api.reclaim_session = False
        yield api


@pytest.fixture
def config_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="200002",
        data={
            CONF_PHONE: PHONE,
            CONF_PASSWORD: "pw",
            CONF_CLIENT_ID: "cid",
            CONF_SESSION: {
                "token": "tok",
                "user_id": 200002,
                "refresh_token": "rt",
                "login_version": 0,
            },
        },
    )
