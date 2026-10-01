"""Diagnostics for York Smart Home.

Built from an allowlist: only protocol, capability and state fields are
exported, never account data, identifiers, names or locations.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import (
    CONF_SESSION,
    DEFLECTOR_NUMBERS,
    FAN_MODES,
    KEY_COMFORTABLE,
    KEY_ENERGY_SAVING,
    KEY_FILTER_RESET,
    KEY_FRESH_AIR_CONTROLLER,
    KEY_HALF_DEGREE_SUPPORTED,
    KEY_HUMAN_SENSE_TYPE,
    KEY_HUMAN_SENSE_TYPE2,
    KEY_HUMIDIFICATION,
    KEY_INDOOR_CO2,
    KEY_INDOOR_HUMIDITY,
    KEY_INDOOR_PM25,
    KEY_INDOOR_TEMP,
    KEY_INDOOR_TVOC,
    KEY_K3_EFFECT,
    KEY_K3_STATE,
    KEY_K3_TIME,
    KEY_ONE_DIRECTION_HUMAN_SENSE,
    KEY_OTA,
    KEY_POWER,
    KEY_ROUND_TIME,
    KEY_SELF_CLEAN,
    KEY_SET_HUMIDITY,
    KEY_SET_TEMP,
    KEY_SET_TEMP_HALF,
    KEY_SIMPLE_TIMER_TIME,
    KEY_STERILIZATION,
    KEY_THREE_AIR_SUPPLY,
    KEY_TIMER_SWITCH,
    KEY_TIMESTAMP,
    KEY_ULTRA_FAR_AIR_SUPPLY,
    LOCK_FAN,
    LOCK_MODE,
    LOCK_TEMP,
    MODES,
    SCENE_FLAG_KEYS,
    SETTINGS_KEYS,
    STANDARD_STATUS_BANNERS,
    STATUS_BANNERS,
    TEMP_LIMIT_COOL,
    TEMP_LIMIT_HEAT,
    TIMER_KEYS,
)
from .coordinator import YorkConfigEntry, YorkScene
from .profile import FUNC_KEYS
from .scenes import RUN

_INFO_KEYS = (
    "type",
    "xkqType",
    "brand",
    "productName",
    "versionWifi",
    "versionMcu",
    "halfSupport",
    "coldMin",
    "coldMax",
    "heatMin",
    "heatMax",
)
_STATUS_KEYS = {
    KEY_POWER,
    KEY_SET_HUMIDITY,
    KEY_SET_TEMP,
    KEY_SET_TEMP_HALF,
    KEY_INDOOR_TEMP,
    KEY_HALF_DEGREE_SUPPORTED,
    KEY_TIMESTAMP,
    KEY_ROUND_TIME,
    KEY_OTA,
    KEY_ONE_DIRECTION_HUMAN_SENSE,
    KEY_FRESH_AIR_CONTROLLER,
    LOCK_MODE,
    LOCK_TEMP,
    LOCK_FAN,
    KEY_FILTER_RESET,
    *SETTINGS_KEYS,
    # Status banners, running state (masking overlay) and 3D human sensor.
    *STATUS_BANNERS,
    KEY_SELF_CLEAN,
    KEY_STERILIZATION,
    KEY_COMFORTABLE,
    KEY_ENERGY_SAVING,
    KEY_HUMAN_SENSE_TYPE2,
    *STANDARD_STATUS_BANNERS,
    # Extra functions, humidity, human sensing and air deflectors (batch 5).
    *(key for key, _ in FUNC_KEYS.values()),
    KEY_SET_HUMIDITY,
    KEY_HUMIDIFICATION,
    KEY_HUMAN_SENSE_TYPE,
    # Fan-lock flag; also hides 智慧人感 / 风吹人 / 风避人 (has_smart_people).
    "B9",
    KEY_K3_EFFECT,
    KEY_K3_STATE,
    KEY_K3_TIME,
    KEY_THREE_AIR_SUPPLY,
    KEY_ULTRA_FAR_AIR_SUPPLY,
    *(f"position_of_air_deflector{n}" for n in DEFLECTOR_NUMBERS),
    *(f"section_of_air_deflector{n}" for n in DEFLECTOR_NUMBERS),
    *(f"air_deflector{n}_independent_or_together_ctrl" for n in DEFLECTOR_NUMBERS),
    # Fresh-air controller tiles (room air, no identifiers).
    KEY_INDOOR_HUMIDITY,
    KEY_INDOOR_PM25,
    KEY_INDOOR_CO2,
    KEY_INDOOR_TVOC,
    # Simple timer: state (timerSimple*) and the fields of its ctrlJson.
    *TIMER_KEYS,
    KEY_TIMER_SWITCH,
    KEY_SIMPLE_TIMER_TIME,
    "protocol_version",
    "mcu_software_version",
    "wifi_software_version",
    *TEMP_LIMIT_COOL,
    *TEMP_LIMIT_HEAT,
    *(key for pair in MODES.values() for key in pair),
    *(key for pair in FAN_MODES.values() for key in pair),
}
# Alarm slots: code plus system/indoor unit number (bus positions, not IDs).
_STATUS_PATTERNS = re.compile(
    r"iu\d+_(alarm_code|system_no|addr_no)|filter_life_tips\d|is_\w+_avalable"
)
# A controller's entry of the firmware answer (getOtas) and its offered
# updates: flags, types and release notes. Never the xkqCode, the update's
# otaId, nor the indoor unit ids of its ouIuList (only counted). Only plain
# values are exported (_plain): the app never reads statusProgress and
# several others, so what they may hold is unknown.
_FIRMWARE_KEYS = ("otaFlg", "statusProgress")
_OTA_KEYS = (
    "otaType",
    "otaDesc",
    "confirmFlg",
    "confirmTime",
    "otaProcess",
    "popupFlg",
    "romVersionD",
)
# Capability, lock and limit flags of the controller constants.
_CONSTANT_PATTERNS = re.compile(
    r"is_\w+_(avalable|available|forbidden)|\w+_fix|controller\w+Fix"
    r"|\w+_temp_(down|up)|\w+_air_deflector_num|human_sense_type2?"
    r"|air_deflector_independent_or_together_ctrl"
    r"|wire_controller_wind_(silent|infinite)|remote_device_type|ac_brand|has_\w+"
)


def _filter(values: dict[str, Any], patterns: re.Pattern[str]) -> dict[str, Any]:
    return {
        key: value
        for key, value in sorted(values.items())
        if key in _STATUS_KEYS or patterns.fullmatch(key)
    }


def _entries(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _plain(value: Any, code: str) -> Any:
    """A number, flag or null as it is, a text without ``code``; else type and size.

    For answer fields whose shape is unknown: a structure could hold ids
    (e.g. indoor unit ids, which start with the controller's code), so it
    is left out, and the controller's code is taken out of texts.
    """
    if isinstance(value, str):
        return value.replace(code, "<xkqCode>") if code else value
    if value is None or isinstance(value, int | float | bool):
        return value
    if isinstance(value, dict | list):
        return f"<{type(value).__name__} of {len(value)}>"
    return f"<{type(value).__name__}>"


def _firmware(entry: dict[str, Any] | None, code: str) -> dict[str, Any] | None:
    """A controller's entry of the last firmware answer (None: not read yet)."""
    if entry is None:
        return None
    updates = []
    for update in _entries(entry.get("otaList")):
        allowed = {key: _plain(update[key], code) for key in _OTA_KEYS if key in update}
        if isinstance(update.get("ouIuList"), list):
            allowed["indoor_unit_count"] = len(update["ouIuList"])
        updates.append(allowed)
    return {
        # Whether the answer had an entry for the controller at all.
        "listed": bool(entry),
        **{key: _plain(entry[key], code) for key in _FIRMWARE_KEYS if key in entry},
        "otaList": updates,
    }


def _scene(scene: YorkScene) -> dict[str, Any]:
    """A cloud scene without its name, ids, schedule or tokens.

    Wire controllers by type with their stored command (allowlisted status
    keys only); gateway indoor units are only counted.
    """
    record = scene.record
    stype = scene.stype
    entity = None if stype is None else "button" if stype.control == RUN else "switch"
    return {
        "type": record.get("type"),
        "entity": entity,
        "present": scene.present,
        "offered": scene.offered,
        "openFlg": record.get("openFlg"),
        "actionFlg": record.get("actionFlg"),
        "favoriteFlg": record.get("favoriteFlg"),
        "wire_controllers": [
            {
                "xkqType": entry.get("xkqType"),
                "deviceType": entry.get("deviceType"),
                "ctrlJson": _filter(entry["ctrlJson"], _STATUS_PATTERNS)
                if isinstance(entry.get("ctrlJson"), dict)
                else None,
            }
            for entry in _entries(record.get("xkqList"))
        ],
        "indoor_unit_count": len(_entries(record.get("iuList"))),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: YorkConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "login_method": "password" if entry.data.get(CONF_PASSWORD) else "sms",
        "has_session": bool(entry.data.get(CONF_SESSION)),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "session_yielded": coordinator.session_yielded,
        "client_outdated": coordinator.client_outdated,
        "unsupported_device_count": coordinator.api.unsupported_count,
        "devices": [
            {
                "device": index,
                "online": dev.online,
                "present": dev.present,
                "info": {k: dev.info[k] for k in _INFO_KEYS if k in dev.info},
                "status": _filter(dev.status, _STATUS_PATTERNS),
                "constants": _filter(dev.constants, _CONSTANT_PATTERNS),
                "firmware": _firmware(dev.firmware, dev.code),
            }
            for index, dev in enumerate(coordinator.devices.values(), start=1)
        ],
        # Whether the last firmware read (getOtas) failed.
        "firmware_read_failed": coordinator.firmware_error is not None,
        # Cloud scenes per home (homes by index, no ids or names). Only
        # whether the last read failed: the error names the home in its URL.
        "scene_read_failed": coordinator.scene_error is not None,
        "homes": [
            {
                "home": index,
                "present": home.present,
                "scenes_loaded": home.loaded,
                # Feature flags only (booleans or "0"/"1").
                "flags": {k: v for k, v in home.flags.items() if k in SCENE_FLAG_KEYS},
                "scenes": [_scene(scene) for scene in home.scenes.values()],
            }
            for index, home in enumerate(coordinator.homes.values(), start=1)
        ],
    }
