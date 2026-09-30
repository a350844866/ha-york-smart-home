"""Settings entities: filter reset button, key lock / key sound / auto brightness
switches and the brightness numbers (xkq-color-set save(), filterScreenReset)."""

from __future__ import annotations

import copy
import time
from unittest.mock import patch

import pytest

from custom_components.york_smart_home.api import (
    YorkAuthError,
    YorkCommandError,
    YorkConnectionError,
    YorkSessionTakenError,
)
from custom_components.york_smart_home.const import DOMAIN
from custom_components.york_smart_home.control import (
    auto_brightness,
    build_settings,
    build_standard_settings,
    filter_reset_command,
    filter_reset_state,
    has_color_settings,
    has_filter_tips,
    has_standard_settings,
    standard_brightness_level,
)
from custom_components.york_smart_home.diagnostics import async_get_config_entry_diagnostics
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import CODE, FIXTURE

SETTINGS_ORDER = [
    "key_lock",
    "key_sound_switch",
    "indicator_light_mode",
    "backlight_brightness_mode",
    "indicator_light_brightness",
    "backlight_brightness",
]
# Keys the settings entities depend on.
_SETTINGS_STATUS_KEYS = {
    *SETTINGS_ORDER,
    *(f"filter_life_tips{i}" for i in range(1, 6)),
}


def _view(**overrides) -> dict:
    return {**FIXTURE["device"]["constantJson"], **FIXTURE["status"], **overrides}


def _with_status(mock_api, status: dict) -> None:
    mock_api.get_status.side_effect = lambda home, codes: {CODE: copy.deepcopy(status)}


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{CODE}_{key}")


def _last_ctrl(mock_api) -> dict:
    return mock_api.control.call_args.args[4]


async def _service(hass, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


def _later(seconds: float = 60):
    return patch(
        "custom_components.york_smart_home.coordinator.time.monotonic",
        return_value=time.monotonic() + seconds,
    )


def _manual(**overrides) -> dict:
    """The fixture status with auto brightness off."""
    return {
        **FIXTURE["status"],
        "indicator_light_mode": "0",
        "backlight_brightness_mode": "0",
        **overrides,
    }


def _reauth_started(hass: HomeAssistant) -> bool:
    return any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def _yield(mock_api, entry) -> None:
    """The app logs in: the coordinator leaves the session to it."""
    good = mock_api.get_status.side_effect
    mock_api.get_status.side_effect = YorkSessionTakenError("HTTP 401")
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.session_yielded
    mock_api.get_status.side_effect = good


# ------------------------------------------------------------ command builders


def test_settings_bundle_from_fixture() -> None:
    """save() always sends all six keys, with the app's types."""
    cmd = build_settings(_view(), key_lock=True)
    assert cmd == {
        "key_lock": "1",
        "key_sound_switch": "1",
        "indicator_light_mode": "1",
        "backlight_brightness_mode": "1",
        "indicator_light_brightness": 30,
        "backlight_brightness": 30,
    }
    assert list(cmd) == SETTINGS_ORDER


def test_settings_bundle_overrides() -> None:
    view = _view(indicator_light_mode="0", backlight_brightness_mode="0")
    assert build_settings(view, key_sound=False)["key_sound_switch"] == "0"
    assert build_settings(view, backlight=55) == {
        "key_lock": "0",
        "key_sound_switch": "1",
        "indicator_light_mode": "0",
        "backlight_brightness_mode": "0",
        "indicator_light_brightness": 30,
        "backlight_brightness": 55,
    }
    assert build_settings(view, indicator=1)["indicator_light_brightness"] == 1
    # The auto toggle drives both mode keys.
    on = build_settings(view, auto=True)
    assert on["indicator_light_mode"] == on["backlight_brightness_mode"] == "1"
    off = build_settings(_view(), auto=False)
    assert off["indicator_light_mode"] == off["backlight_brightness_mode"] == "0"


def test_settings_bundle_keeps_reported_values() -> None:
    # Unchanged brightness goes out exactly as reported (the page's model);
    # integer flags are read loosely but sent as the app's strings.
    view = _view(key_lock=1, key_sound_switch=0, backlight_brightness="45")
    cmd = build_settings(view, auto=False)
    assert cmd["key_lock"] == "1" and cmd["key_sound_switch"] == "0"
    assert cmd["backlight_brightness"] == "45"
    # A brightness the controller does not report is left out (undefined
    # in the app, dropped by JSON.stringify).
    view = _view()
    del view["indicator_light_brightness"]
    assert "indicator_light_brightness" not in build_settings(view, key_lock=False)


def test_auto_brightness_rule() -> None:
    # Auto unless both mode keys are "0" (settings page ngOnInit).
    assert auto_brightness(_view())
    assert not auto_brightness(_view(indicator_light_mode="0", backlight_brightness_mode="0"))
    assert auto_brightness(_view(indicator_light_mode="0", backlight_brightness_mode="1"))
    assert auto_brightness(_view(indicator_light_mode="1", backlight_brightness_mode="0"))
    view = _view(backlight_brightness_mode="0")
    del view["indicator_light_mode"]
    assert auto_brightness(view)  # missing reads as auto, like undefined !== "0"
    # Mixed modes are normalized by the next save.
    mixed = build_settings(_view(indicator_light_mode="0"), key_lock=False)
    assert mixed["indicator_light_mode"] == mixed["backlight_brightness_mode"] == "1"


def test_filter_reset_builders() -> None:
    assert filter_reset_command() == {"screen_mark_reset": "1"}
    assert has_filter_tips(_view())
    assert not has_filter_tips({k: v for k, v in _view().items() if "filter_life" not in k})
    assert filter_reset_state(_view()) == {}  # nothing due
    due = _view(
        filter_life_tips1=2, filter_life_tips3=3, filter_life_tips4="2", filter_life_tips5=3
    )
    # Slot 3 also counts 3 (needs replacing); types are kept.
    assert filter_reset_state(due) == {
        "filter_life_tips1": 0,
        "filter_life_tips3": 0,
        "filter_life_tips4": "0",
    }


# ------------------------------------------------------------ entity creation


async def test_settings_entities_created(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    ent_reg = er.async_get(hass)
    expected = {
        (BUTTON_DOMAIN, "filter_reset"): "unknown",
        (SWITCH_DOMAIN, "key_lock"): "off",
        (SWITCH_DOMAIN, "key_sound_switch"): "on",
        (SWITCH_DOMAIN, "auto_brightness"): "on",
        (NUMBER_DOMAIN, "backlight_brightness"): "30.0",
        (NUMBER_DOMAIN, "indicator_light_brightness"): "30.0",
    }
    for (platform, key), state in expected.items():
        entity_id = _entity(hass, platform, key)
        assert entity_id, key
        assert hass.states.get(entity_id).state == state, key
        assert ent_reg.async_get(entity_id).entity_category is EntityCategory.CONFIG
    attrs = hass.states.get(_entity(hass, NUMBER_DOMAIN, "backlight_brightness")).attributes
    assert (attrs["min"], attrs["max"], attrs["step"], attrs["mode"]) == (1, 100, 1, "slider")


async def test_settings_entities_need_their_keys(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    status = {k: v for k, v in FIXTURE["status"].items() if k not in _SETTINGS_STATUS_KEYS}
    status["key_sound_switch"] = "0"
    status["backlight_brightness"] = 20
    status["backlight_brightness_mode"] = "0"  # only one of the two mode keys
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    assert _entity(hass, BUTTON_DOMAIN, "filter_reset") is None
    assert _entity(hass, SWITCH_DOMAIN, "key_lock") is None
    assert _entity(hass, SWITCH_DOMAIN, "auto_brightness") is None
    assert _entity(hass, NUMBER_DOMAIN, "indicator_light_brightness") is None
    # Reported, but with a mode key missing auto brightness always reads as
    # on and could not be turned off: a slider that can never be written.
    assert _entity(hass, NUMBER_DOMAIN, "backlight_brightness") is None
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, "key_sound_switch")).state == "off"

    # The bundle leaves out what the controller does not report.
    await _service(
        hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_sound_switch")
    )
    assert _last_ctrl(mock_api) == {
        "key_lock": "0",
        "key_sound_switch": "1",
        "indicator_light_mode": "1",
        "backlight_brightness_mode": "1",
        "backlight_brightness": 20,
    }


async def test_brightness_without_indicator(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Both mode keys but no indicator brightness: only the backlight slider."""
    status = _manual()
    del status["indicator_light_brightness"]
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    assert _entity(hass, NUMBER_DOMAIN, "indicator_light_brightness") is None
    assert _entity(hass, SWITCH_DOMAIN, "auto_brightness")
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 40})
    assert _last_ctrl(mock_api) == {
        "key_lock": "0",
        "key_sound_switch": "1",
        "indicator_light_mode": "0",
        "backlight_brightness_mode": "0",
        "backlight_brightness": 40,
    }


async def test_standard_controller_settings(hass: HomeAssistant, mock_api, config_entry) -> None:
    """xkqType 8 (xkq-set): four keys, brightness as the level index 0..5.

    save() sends key_lock, backlight_brightness (0 while auto),
    backlight_brightness_mode and key_sound_switch; there is no indicator
    light. The filter reset is the same request on its control page.
    """
    assert has_color_settings("9") and not has_color_settings("8")
    assert has_standard_settings("8") and not has_standard_settings("9")
    mock_api.get_wire_controllers.return_value = [{**FIXTURE["device"], "xkqType": "8"}]
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.devices[CODE].xkq_type == "8"
    assert _entity(hass, NUMBER_DOMAIN, "indicator_light_brightness") is None
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    state = hass.states.get(backlight)
    assert state.state == "unknown"  # auto brightness (mode "1")
    assert state.attributes["min"] == 1 and state.attributes["max"] == 6
    auto = _entity(hass, SWITCH_DOMAIN, "auto_brightness")
    assert hass.states.get(auto).state == "on"

    # Turning auto off keeps a valid reported level, else 一档 (index 0).
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, auto)
    assert _last_ctrl(mock_api) == {
        "key_lock": "0",
        "backlight_brightness": 0,
        "backlight_brightness_mode": "0",
        "key_sound_switch": "1",
    }
    assert mock_api.control.call_args.args[3] == "8"

    # Choosing a level: one selector with "自动" on the page, so auto goes off.
    _with_status(mock_api, {**FIXTURE["status"], "backlight_brightness": 3})
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 5})
    assert _last_ctrl(mock_api) == {
        "key_lock": "0",
        "backlight_brightness": 4,
        "backlight_brightness_mode": "0",
        "key_sound_switch": "1",
    }

    manual = {**FIXTURE["status"], "backlight_brightness": 2, "backlight_brightness_mode": "0"}
    _with_status(mock_api, manual)
    with _later():  # after the grace period of the commands above
        await config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(backlight).state == "3"
    mock_api.control.reset_mock()
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 3})
    mock_api.control.assert_not_called()  # already so
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, auto)
    assert _last_ctrl(mock_api) == {
        "key_lock": "0",
        "backlight_brightness": 0,
        "backlight_brightness_mode": "1",
        "key_sound_switch": "1",
    }
    # The auto brightness just sent is held against the older report.
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_lock"))
    assert _last_ctrl(mock_api) == {
        "key_lock": "1",
        "backlight_brightness": 0,
        "backlight_brightness_mode": "1",
        "key_sound_switch": "1",
    }
    await _service(hass, BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"))
    assert _last_ctrl(mock_api) == {"screen_mark_reset": "1"}


def test_standard_settings_bundle() -> None:
    view = _view(backlight_brightness_mode="0", backlight_brightness=5)
    assert standard_brightness_level(view) == 6
    assert (
        standard_brightness_level(_view(backlight_brightness_mode="0", backlight_brightness=6))
        is None
    )
    assert standard_brightness_level(_view()) is None  # auto
    # Only backlight_brightness_mode decides (no indicator key there).
    assert not auto_brightness(_view(backlight_brightness_mode="0"), standard=True)
    assert auto_brightness(_view(backlight_brightness_mode="0"))
    assert list(build_standard_settings(view, key_sound=False)) == [
        "key_lock",
        "backlight_brightness",
        "backlight_brightness_mode",
        "key_sound_switch",
    ]
    assert build_standard_settings(view, key_sound=False)["backlight_brightness"] == 5
    assert build_standard_settings(view, auto=True)["backlight_brightness"] == 0
    assert build_standard_settings(view, level=1)["backlight_brightness"] == 0


async def test_settings_not_from_constants(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The settings page reads only the live status, never constantJson."""
    constants = {
        **FIXTURE["device"]["constantJson"],
        "key_lock": "1",
        "backlight_brightness": 99,
        "filter_life_tips1": 2,
    }
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: copy.deepcopy(constants)
    await _setup(hass, config_entry)
    key_lock = _entity(hass, SWITCH_DOMAIN, "key_lock")
    assert hass.states.get(key_lock).state == "off"
    assert hass.states.get(_entity(hass, NUMBER_DOMAIN, "backlight_brightness")).state == "30.0"
    assert hass.states.get(_entity(hass, "binary_sensor", "filter")).state == "off"
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert _last_ctrl(mock_api)["key_lock"] == "1"
    assert _last_ctrl(mock_api)["backlight_brightness"] == 30
    # ...and the device follows the status, not the constant.
    _with_status(mock_api, {**FIXTURE["status"], "key_lock": "0"})
    with _later():
        await config_entry.runtime_data.async_refresh()
    assert hass.states.get(key_lock).state == "off"


async def test_settings_entities_wait_for_first_status(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A controller whose first status poll brought nothing gets them later."""
    mock_api.get_status.side_effect = lambda home, codes: {}
    await _setup(hass, config_entry)
    assert _entity(hass, SWITCH_DOMAIN, "key_lock") is None
    _with_status(mock_api, FIXTURE["status"])
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, "key_lock")).state == "off"
    assert _entity(hass, BUTTON_DOMAIN, "filter_reset")
    assert _entity(hass, NUMBER_DOMAIN, "backlight_brightness")


# ------------------------------------------------------------------- commands


async def test_switch_commands(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    key_lock = _entity(hass, SWITCH_DOMAIN, "key_lock")
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert mock_api.control.call_args.args[:4] == (100001, CODE, "C", "9")
    cmd = _last_ctrl(mock_api)
    assert cmd == {
        "key_lock": "1",
        "key_sound_switch": "1",
        "indicator_light_mode": "1",
        "backlight_brightness_mode": "1",
        "indicator_light_brightness": 30,
        "backlight_brightness": 30,
    }
    assert list(cmd) == SETTINGS_ORDER
    assert hass.states.get(key_lock).state == "on"  # optimistic

    await _service(
        hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, _entity(hass, SWITCH_DOMAIN, "key_sound_switch")
    )
    assert _last_ctrl(mock_api) == {
        "key_lock": "1",  # the previous change is part of the model now
        "key_sound_switch": "0",
        "indicator_light_mode": "1",
        "backlight_brightness_mode": "1",
        "indicator_light_brightness": 30,
        "backlight_brightness": 30,
    }

    auto = _entity(hass, SWITCH_DOMAIN, "auto_brightness")
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, auto)
    assert _last_ctrl(mock_api) == {
        "key_lock": "1",
        "key_sound_switch": "0",
        "indicator_light_mode": "0",
        "backlight_brightness_mode": "0",
        "indicator_light_brightness": 30,
        "backlight_brightness": 30,
    }
    assert hass.states.get(auto).state == "off"

    # Manual brightness now accepted; the value goes out as an integer.
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 64})
    assert _last_ctrl(mock_api) == {
        "key_lock": "1",
        "key_sound_switch": "0",
        "indicator_light_mode": "0",
        "backlight_brightness_mode": "0",
        "indicator_light_brightness": 30,
        "backlight_brightness": 64,
    }
    assert isinstance(_last_ctrl(mock_api)["backlight_brightness"], int)
    assert hass.states.get(backlight).state == "64.0"
    indicator = _entity(hass, NUMBER_DOMAIN, "indicator_light_brightness")
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, indicator, **{ATTR_VALUE: 1})
    assert _last_ctrl(mock_api)["indicator_light_brightness"] == 1
    assert _last_ctrl(mock_api)["backlight_brightness"] == 64

    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, auto)
    cmd = _last_ctrl(mock_api)
    assert cmd["indicator_light_mode"] == cmd["backlight_brightness_mode"] == "1"
    assert cmd["indicator_light_brightness"] == 1 and cmd["backlight_brightness"] == 64


async def test_settings_read_fresh_state(hass: HomeAssistant, mock_api, config_entry) -> None:
    """The bundle is built from a full re-read, not the last poll.

    The settings page loads its model with getXkqControlData timestamp 0
    before save() sends it all back; a change made on the wall controller
    since the last poll must not be reverted.
    """
    await _setup(hass, config_entry)
    ts = FIXTURE["status"]["timestamp"]
    _with_status(
        mock_api,
        {
            **FIXTURE["status"],
            "timestamp": ts + 1000,
            "roundTime": ts + 1000,
            "key_sound_switch": "0",
            "backlight_brightness": 70,
        },
    )
    order = []
    mock_api.get_status.side_effect = lambda home, codes, f=mock_api.get_status.side_effect: (
        order.append(("status", codes)) or f(home, codes)
    )
    mock_api.control.side_effect = lambda *args: order.append(("control", args[4]))
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_lock"))
    assert order[0] == ("status", [(CODE, 0)])
    assert order[1] == (
        "control",
        {
            "key_lock": "1",
            "key_sound_switch": "0",
            "indicator_light_mode": "1",
            "backlight_brightness_mode": "1",
            "indicator_light_brightness": 30,
            "backlight_brightness": 70,
        },
    )
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, "key_sound_switch")).state == "off"


async def test_settings_fresh_read_refusals(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, _manual())
    await _setup(hass, config_entry)
    key_lock = _entity(hass, SWITCH_DOMAIN, "key_lock")
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    ts = FIXTURE["status"]["timestamp"]

    # Auto brightness was turned on at the controller meanwhile.
    _with_status(
        mock_api,
        _manual(timestamp=ts + 1000, roundTime=ts + 1000, backlight_brightness_mode="1"),
    )
    with pytest.raises(ServiceValidationError) as info:
        await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 50})
    assert info.value.translation_key == "auto_brightness_on"
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, "auto_brightness")).state == "on"

    # The re-read fails or brings nothing: never build from old state.
    mock_api.get_status.side_effect = YorkConnectionError("timeout")
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert info.value.translation_key == "cannot_connect"
    mock_api.get_status.side_effect = lambda home, codes: {}
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert info.value.translation_key == "cannot_connect"

    # The controller started a firmware update.
    _with_status(mock_api, _manual(timestamp=ts + 2000, roundTime=ts + 2000, otaFlg="1"))
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert info.value.translation_key == "ota_in_progress"
    mock_api.control.assert_not_called()


async def test_settings_no_op(hass: HomeAssistant, mock_api, config_entry) -> None:
    """Re-asserting the current value sends nothing (like build_command's {})."""
    _with_status(mock_api, _manual())
    await _setup(hass, config_entry)
    await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, _entity(hass, SWITCH_DOMAIN, "key_lock"))
    await _service(
        hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_sound_switch")
    )
    await _service(
        hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, _entity(hass, SWITCH_DOMAIN, "auto_brightness")
    )
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 30})
    mock_api.control.assert_not_called()
    assert not config_entry.runtime_data.devices[CODE].expected


async def test_settings_no_op_auto(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)  # auto brightness on
    await _service(
        hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "auto_brightness")
    )
    mock_api.control.assert_not_called()


@pytest.mark.parametrize(("value", "sent"), [(2.5, 3), (1.5, 2), (64.4, 64), (99.6, 100)])
async def test_brightness_fractional(
    hass: HomeAssistant, mock_api, config_entry, value: float, sent: int
) -> None:
    _with_status(mock_api, _manual())
    await _setup(hass, config_entry)
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: value})
    assert _last_ctrl(mock_api)["backlight_brightness"] == sent
    assert type(_last_ctrl(mock_api)["backlight_brightness"]) is int


async def test_brightness_refused_while_auto(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    with pytest.raises(ServiceValidationError) as info:
        await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 50})
    assert info.value.translation_key == "auto_brightness_on"
    mock_api.control.assert_not_called()
    # Only one mode key on also counts as auto.
    _with_status(mock_api, {**FIXTURE["status"], "backlight_brightness_mode": "0"})
    coordinator = config_entry.runtime_data
    coordinator._last_discovery = None  # full status
    await coordinator.async_refresh()
    with pytest.raises(ServiceValidationError):
        await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 50})
    mock_api.control.assert_not_called()


async def test_settings_with_extra_capabilities(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Functions that the climate commands cancel are never part of the bundle."""
    constants = {
        **FIXTURE["device"]["constantJson"],
        "is_energy_conservation_avalable": "1",
        "is_indoor_unit_silent_avalable": "1",
        "is_forest_wind_avalable": "1",
        "is_floor_heating_avalable": "1",
        "is_air_deflector_avalable": "1",
        "vertical_air_deflector_num": "2",
    }
    mock_api.get_wire_controller_detail.side_effect = lambda home, code: copy.deepcopy(constants)
    status = {
        **FIXTURE["status"],
        "energy_conservation": "1",
        "indoor_unit_silent": "1",
        "one_direction_human_sense": "1",
        "key_lock": 1,  # integer flags read loosely
        "indicator_light_mode": 0,
        "backlight_brightness_mode": "0",
        "backlight_brightness": "80",
        "filter_life_tips3": 3,
    }
    _with_status(mock_api, status)
    await _setup(hass, config_entry)
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, "key_lock")).state == "on"
    assert hass.states.get(_entity(hass, SWITCH_DOMAIN, "auto_brightness")).state == "off"

    indicator = _entity(hass, NUMBER_DOMAIN, "indicator_light_brightness")
    await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, indicator, **{ATTR_VALUE: 12})
    assert _last_ctrl(mock_api) == {
        "key_lock": "1",
        "key_sound_switch": "1",
        "indicator_light_mode": "0",
        "backlight_brightness_mode": "0",
        "indicator_light_brightness": 12,
        "backlight_brightness": "80",
    }
    await _service(hass, BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"))
    assert _last_ctrl(mock_api) == {"screen_mark_reset": "1"}
    # Nothing else of the device was touched.
    view = config_entry.runtime_data.devices[CODE].view
    assert view["energy_conservation"] == "1" and view["one_direction_human_sense"] == "1"


# --------------------------------------------------------------- filter reset


async def test_filter_reset(hass: HomeAssistant, mock_api, config_entry) -> None:
    due = {**FIXTURE["status"], "filter_life_tips1": 2}
    _with_status(mock_api, due)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    reminder = _entity(hass, "binary_sensor", "filter")
    assert hass.states.get(reminder).state == "on"

    # Incremental polling in between.
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, due["roundTime"])]

    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await _service(
            hass, BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset")
        )
    assert _last_ctrl(mock_api) == {"screen_mark_reset": "1"}
    assert mock_api.control.call_args.args[:4] == (100001, CODE, "C", "9")
    assert mock_api.get_status.call_count == 2  # no re-read before a reset
    assert hass.states.get(reminder).state == "off"  # cleared right away
    status = coordinator.devices[CODE].status
    assert status["filter_life_tips1"] == 0
    assert status["screen_mark_reset"] == "0"  # the flag itself is not kept

    # Within the grace period polling stays incremental, and a report still
    # showing the reminder does not bring it back (the controller may not
    # have applied the reset yet)...
    ts = due["timestamp"]
    _with_status(mock_api, {**due, "timestamp": ts + 1000, "roundTime": ts + 1000})
    await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, ts)]
    assert hass.states.get(reminder).state == "off"

    # ...then the first poll after it reads the full state once, and the
    # controller's own tips decide: here the reset did not take.
    _with_status(mock_api, {**due, "timestamp": ts + 2000, "roundTime": ts + 2000})
    with _later():
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
    assert hass.states.get(reminder).state == "on"
    with _later():
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, ts + 2000)]


async def test_filter_reset_confirmed(hass: HomeAssistant, mock_api, config_entry) -> None:
    due = {**FIXTURE["status"], "filter_life_tips3": 3}
    _with_status(mock_api, due)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    reminder = _entity(hass, "binary_sensor", "filter")
    assert hass.states.get(reminder).state == "on"
    await _service(hass, BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"))
    assert hass.states.get(reminder).state == "off"
    ts = due["timestamp"]
    _with_status(
        mock_api, {**due, "timestamp": ts + 2000, "roundTime": ts + 2000, "filter_life_tips3": 0}
    )
    with _later():
        await coordinator.async_refresh()
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
    assert hass.states.get(reminder).state == "off"
    assert coordinator.devices[CODE].recheck_after is None


async def test_filter_reset_not_due(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    await _service(hass, BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"))
    assert _last_ctrl(mock_api) == {"screen_mark_reset": "1"}
    assert not config_entry.runtime_data.devices[CODE].expected  # nothing to hold


# ------------------------------------------------------- refusal and grace


def _all_actions(hass):
    return [
        (BUTTON_DOMAIN, SERVICE_PRESS, _entity(hass, BUTTON_DOMAIN, "filter_reset"), {}),
        (SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_lock"), {}),
        (SWITCH_DOMAIN, SERVICE_TURN_OFF, _entity(hass, SWITCH_DOMAIN, "auto_brightness"), {}),
    ]


async def test_settings_refused_offline(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    actions = _all_actions(hass)
    mock_api.get_online.return_value = {}  # missing counts as offline
    coordinator._last_online = None
    await coordinator.async_refresh()
    for domain, service, entity_id, data in actions:
        assert hass.states.get(entity_id).state == "unavailable"
        # HA skips unavailable entities; nothing is sent.
        await _service(hass, domain, service, entity_id, **data)
    with pytest.raises(HomeAssistantError) as info:
        await coordinator.async_send_raw(CODE, lambda view: {"key_lock": "1"})
    assert info.value.translation_key == "offline"
    mock_api.control.assert_not_called()


async def test_settings_refused_during_ota(hass: HomeAssistant, mock_api, config_entry) -> None:
    _with_status(mock_api, {**FIXTURE["status"], "otaFlg": "1"})
    await _setup(hass, config_entry)
    for domain, service, entity_id, data in _all_actions(hass):
        with pytest.raises(HomeAssistantError) as info:
            await _service(hass, domain, service, entity_id, **data)
        assert info.value.translation_key == "ota_in_progress"
    mock_api.control.assert_not_called()


async def test_settings_grace(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    key_lock = _entity(hass, SWITCH_DOMAIN, "key_lock")
    auto = _entity(hass, SWITCH_DOMAIN, "auto_brightness")
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, auto)

    # A report from before the change does not flip them back...
    ts = FIXTURE["status"]["timestamp"]
    stale = {**FIXTURE["status"], "timestamp": ts + 1000, "roundTime": ts + 1000}
    _with_status(mock_api, stale)
    await coordinator.async_refresh()
    assert hass.states.get(key_lock).state == "on"
    assert hass.states.get(auto).state == "off"

    # ...a confirming report ends the hold for that key...
    confirmed = {**stale, "timestamp": ts + 2000, "roundTime": ts + 2000, "key_lock": "1"}
    _with_status(mock_api, confirmed)
    await coordinator.async_refresh()
    assert "key_lock" not in coordinator.devices[CODE].expected
    assert "indicator_light_mode" in coordinator.devices[CODE].expected

    # ...and after the grace period the controller's report wins.
    later = {**FIXTURE["status"], "timestamp": ts + 30000, "roundTime": ts + 30000}
    _with_status(mock_api, later)
    with _later():
        await coordinator.async_refresh()
    assert hass.states.get(key_lock).state == "off"
    assert hass.states.get(auto).state == "on"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (YorkCommandError("失败"), "command_rejected"),
        (YorkAuthError("rejected"), "auth_failed"),
        (YorkSessionTakenError("HTTP 401"), "session_taken"),
    ],
)
async def test_settings_command_rejected(
    hass: HomeAssistant, mock_api, config_entry, error: Exception, key: str
) -> None:
    await _setup(hass, config_entry)
    key_lock = _entity(hass, SWITCH_DOMAIN, "key_lock")
    mock_api.control.side_effect = error
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert info.value.translation_key == key
    assert hass.states.get(key_lock).state == "off"  # no optimistic change
    assert not config_entry.runtime_data.devices[CODE].expected
    await hass.async_block_till_done()
    assert _reauth_started(hass) is (key == "auth_failed")
    # The other device keeps the session it took.
    assert config_entry.runtime_data.session_yielded is (key == "session_taken")


# -------------------------------------------- session left to the app


async def test_settings_during_yield_use_reread_state(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A settings command takes the session back and builds from the re-read state."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    await _yield(mock_api, config_entry)
    # Meanwhile the app changed key sound and backlight.
    ts = FIXTURE["status"]["timestamp"]
    _with_status(
        mock_api,
        {
            **FIXTURE["status"],
            "timestamp": ts + 1000,
            "roundTime": ts + 1000,
            "key_sound_switch": "0",
            "backlight_brightness": 70,
        },
    )
    reads = mock_api.get_status.call_count
    with patch("custom_components.york_smart_home.coordinator.REFRESH_DELAY_AFTER_CONTROL", 999):
        await _service(
            hass, SWITCH_DOMAIN, SERVICE_TURN_ON, _entity(hass, SWITCH_DOMAIN, "key_lock")
        )
    mock_api.relogin.assert_awaited_once()
    # One full re-read (the session reclaim's), not a second one.
    assert mock_api.get_status.call_count == reads + 1
    assert mock_api.get_status.call_args.args[1] == [(CODE, 0)]
    assert _last_ctrl(mock_api) == {
        "key_lock": "1",
        "key_sound_switch": "0",
        "indicator_light_mode": "1",
        "backlight_brightness_mode": "1",
        "indicator_light_brightness": 30,
        "backlight_brightness": 70,
    }
    assert not coordinator.session_yielded


async def test_brightness_during_yield_rechecks_auto(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Auto brightness turned on in the app during the yield: refused after the re-read."""
    _with_status(mock_api, _manual())
    await _setup(hass, config_entry)
    await _yield(mock_api, config_entry)
    ts = FIXTURE["status"]["timestamp"]
    _with_status(
        mock_api,
        _manual(timestamp=ts + 1000, roundTime=ts + 1000, backlight_brightness_mode="1"),
    )
    backlight = _entity(hass, NUMBER_DOMAIN, "backlight_brightness")
    with pytest.raises(ServiceValidationError) as info:
        await _service(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, backlight, **{ATTR_VALUE: 50})
    assert info.value.translation_key == "auto_brightness_on"
    mock_api.relogin.assert_awaited_once()
    mock_api.control.assert_not_called()


async def test_settings_during_yield_reread_problems(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    await _setup(hass, config_entry)
    key_lock = _entity(hass, SWITCH_DOMAIN, "key_lock")
    await _yield(mock_api, config_entry)
    mock_api.get_status.side_effect = YorkConnectionError("timeout")
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert info.value.translation_key == "cannot_connect"

    await _yield(mock_api, config_entry)
    _with_status(mock_api, {**FIXTURE["status"], "otaFlg": "1"})
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, key_lock)
    assert info.value.translation_key == "ota_in_progress"
    mock_api.control.assert_not_called()


async def test_settings_in_diagnostics(hass: HomeAssistant, mock_api, config_entry) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    status = diag["devices"][0]["status"]
    for key in SETTINGS_ORDER:
        assert status[key] == FIXTURE["status"][key], key
    assert status["screen_mark_reset"] == "0"
    assert status["filter_life_tips1"] == 1
