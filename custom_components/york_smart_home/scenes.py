"""Cloud scenes (智能场景) of the 约克智慧家 app.

HA-free rules for the scenes the app's scenes tab offers: the control it
shows for each scene type (a run button, an on/off toggle or nothing), how
their state is read, and the request that runs a stored scene. Verified in
the app bundle (es2015 chunks):

* List: the scenes tab (ScenesPage, chunk 15, initPageData) reads GET
  /api/apptimer/homes/{homeId}/smarts (SCENE_GET_ALL_SCENE) and keeps
  ``data.smartList`` when the code is "200". The home detail
  (FAMILY_DETAIL) carries the same records in homeDetail.smartList; the
  home page drops the ones with favoriteFlg "0" itself.
* Controls: the tab's template (chunk 18) gives types 1, 2, F, G, I and J a
  run (play) button (J's button switches its openFlg), every other
  recommended type and the timer list (types 9, M, O) a toggle that is "on"
  only for openFlg "1", the linkage list (B–E) a toggle that is "on" for
  actionFlg "1", the pets scene (H) a toggle that is "on" for actionFlg "1"
  or "2" (HANGUP), and manual scenes (8) a run button. filterMyScenes and
  the list templates never show types 3, 4, A, N and Z.
* Run: there is no "execute scene" call (SCENE_EXECUTE /cmds/sceneControl
  is never used). The app replays the stored commands through
  multiIuOuCtrl (runManualScenes / preControlSceneParams), or posts
  greenhouse and party scenes once to .../scene/type/{type}/{sceneId}.
* Toggle: PUT .../smarts/{smartId}/openFlg {openFlg, updateTime}
  (openCloseScenes); vacation, two-supply dehumidification and motion
  scenes POST .../scene/type/{type}/{sceneId} with forceOpenFlg "0"
  (recommendedSceneExecution); linkage and pets scenes PUT
  .../newScenes/{smartId}/flag/0 with the new actionFlg (runAutoScenes).

The app also hides recommended types whose feature flag in the home detail
is off (6 leaveEnergyFlg, 7 nightQuiteFlg, F kszrFlg, G..L sceneGFlg..
sceneLFlg; 1/2 scene2Flg and 5 scene5Flg only for the placeholders of
scenes not created yet; M sceneMFlg offers the hot-water timer), and the
home page filters its favourites by them. smartSceneFlg only concerns the
ZHP gateways' own smart scenes; smartFlg1/2 and sceneNFlg are never read.
The flags only decide which templates the app offers; a scene that exists
keeps running in the cloud whatever they say, so all existing scenes are
shown here and the home detail is not read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# The control the app's scenes tab shows for a scene type.
RUN = "run"  # a run (play) button: the scene's commands are sent once
SWITCH = "switch"  # an on/off toggle: the cloud runs the scene while it is on

# How the app runs or switches a scene.
REPLAY = "replay"  # multiIuOuCtrl with the scene's stored commands
RECOMMENDED = "recommended"  # POST .../scene/type/{type}/{sceneId}
OPEN_FLAG = "open_flag"  # PUT .../smarts/{smartId}/openFlg
AUTO = "auto"  # PUT .../newScenes/{smartId}/flag/0 (actionFlg)

# openFlg / actionFlg values (the app's JxZl enum).
CLOSE = "0"
OPEN = "1"
HANGUP = "2"
# State of the ``status`` attribute for each flag value.
FLAG_STATUS = {CLOSE: "off", OPEN: "on", HANGUP: "hangup"}


@dataclass(frozen=True)
class SceneType:
    """How the app offers one scene type (the sixH enum)."""

    # State of the scene_type attribute (translated).
    key: str
    # The app's own name for the type, used when a scene has no name.
    name: str
    control: str
    request: str
    # multiIuOuCtrl ctrlType of a REPLAY run.
    ctrl_type: str | None = None
    # A REPLAY run also sends sceneId (AI water/fluorine fast heat only).
    sends_scene_id: bool = False


SCENE_TYPES: dict[str, SceneType] = {
    # Leave / return home: the scenes tab replays them with YO438 like a
    # manual scene; the home page's scene buttons use their own ctrlTypes
    # (SCENE_CONTROL_LEAVE_HOME / _GO_HOME), so the cloud sees which one
    # ran. HA does what the home page does.
    "1": SceneType("leave_home", "离家", RUN, REPLAY, "YO431"),
    "2": SceneType("go_home", "回家", RUN, REPLAY, "YO432"),
    "5": SceneType("sleep_curve", "睡眠曲线", SWITCH, OPEN_FLAG),
    "6": SceneType("leave_home_energy_saving", "离家节能", SWITCH, OPEN_FLAG),
    "7": SceneType("night_mute", "夜间静音", SWITCH, OPEN_FLAG),
    "8": SceneType("manual", "手动场景", RUN, REPLAY, "YO438"),
    # The legacy timer: listed with a toggle, editing it only says
    # "定时场景已升级".
    "9": SceneType("timer_legacy", "定时场景", SWITCH, OPEN_FLAG),
    "B": SceneType("summer", "夏季防暑热场景", SWITCH, AUTO),
    "C": SceneType("winter", "冬季防过冷场景", SWITCH, AUTO),
    "D": SceneType("dehumidify", "除湿智能场景", SWITCH, AUTO),
    "E": SceneType("purify", "净化智能场景", SWITCH, AUTO),
    "F": SceneType("ai_fast_heat", "Ai水氟速热", RUN, REPLAY, "YO43F", sends_scene_id=True),
    "G": SceneType("advance_heating", "提前暖房", RUN, RECOMMENDED),
    "H": SceneType("pets", "宠物场景", SWITCH, AUTO),
    "I": SceneType("party", "聚会场景", RUN, RECOMMENDED),
    # Motion: a run button in the app, which switches openFlg and shows
    # "（执行中）" while it is "1": an on/off state, so a switch here.
    "J": SceneType("motion", "运动场景", SWITCH, RECOMMENDED),
    "K": SceneType("vacation", "度假场景", SWITCH, RECOMMENDED),
    "L": SceneType("smart_dehumidify", "两联供智慧除湿", SWITCH, RECOMMENDED),
    "M": SceneType("hot_water_timer", "热水定时场景", SWITCH, OPEN_FLAG),
    "O": SceneType("timer", "定时场景", SWITCH, OPEN_FLAG),
}

# multiCtrlResult codes (ControlService.getControlResults).
RESULT_SUCCESS = "200"
RESULT_FAILURE = frozenset({"201", "206", "207", "208", "212", "220"})
# Any other code ("205", "400", "500" and unknown ones) keeps it polling.

# The run's iu entries (gateway indoor units): fields copied as stored, and
# the only ctrlJson fields the scenes tab keeps for gateways other than cat
# ("E") and ZHP ("6") units.
_XKQ_KEYS = ("xkqCode", "deviceType", "xkqType", "ctrlJson")
_IU_KEYS = ("systemNo", "iuSn", "iuNo", "iuType")
_IU_CTRL_KEYS = ("onoff", "mode", "temp", "wind", "humi")
_IU_RAW_CTRL_TYPES = ("E", "6")


def scene_type(record: Mapping[str, Any]) -> SceneType | None:
    """The type's rules, or None for types the app does not list."""
    return SCENE_TYPES.get(str(record.get("type")))


def scene_name(record: Mapping[str, Any]) -> str:
    """The scene's name in the app, else the app's name for its type."""
    name = str(record.get("smartName") or "").strip()
    if name:
        return name
    stype = scene_type(record)
    return stype.name if stype else str(record.get("smartId"))


def _flag(value: Any) -> str | None:
    """A "0"/"1"/"2" flag, also sent as a number by some endpoints."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    return text if text in FLAG_STATUS else None


def flag_key(stype: SceneType) -> str:
    """Linkage and pets scenes keep their state in actionFlg, others in openFlg."""
    return "actionFlg" if stype.request == AUTO else "openFlg"


def scene_flag(record: Mapping[str, Any], stype: SceneType) -> str | None:
    return _flag(record.get(flag_key(stype)))


def scene_is_on(record: Mapping[str, Any], stype: SceneType) -> bool:
    """On/off as the app's toggle shows it.

    openFlg: only "1" (every openFlg toggle of the app; HANGUP shows as
    off). actionFlg: "1" and "2" (HANGUP): runAutoScenes switches both off
    with the next tap, the pets toggle shows both as on and its editor
    refuses both with "场景正在执行中". The linkage list's toggle only
    lights up for "1", but its tap treats "2" as running too.
    """
    flag = scene_flag(record, stype)
    if stype.request == AUTO:
        return flag in (OPEN, HANGUP)
    return flag == OPEN


def scene_status(record: Mapping[str, Any], stype: SceneType) -> str | None:
    """The raw flag as "on" / "off" / "hangup" (None if missing or unknown)."""
    flag = scene_flag(record, stype)
    return FLAG_STATUS[flag] if flag is not None else None


def scene_target_reached(record: Mapping[str, Any], stype: SceneType, on: bool) -> bool:
    """Whether switching to ``on`` would change nothing.

    HA sends an explicit "1" or "0" instead of the app's flip of the shown
    state; a hung-up linkage scene already counts as running.
    """
    flag = scene_flag(record, stype)
    if on:
        return flag == OPEN or (stype.request == AUTO and flag == HANGUP)
    return flag == CLOSE


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def scene_device_count(record: Mapping[str, Any]) -> int:
    """Wire controllers plus gateway indoor units in the scene."""
    return len(_list(record.get("xkqList"))) + len(_list(record.get("iuList")))


def has_devices(record: Mapping[str, Any]) -> bool:
    """The app refuses to run or switch on a scene without devices.

    Scenes tab: "请先点击“场景卡片”添加空调" (runManualScenes) and
    "请先编辑该场景并添加空调" (openCloseScenes); home page: "先去场景下添加一台
    空调/设备吧。" (runScene, unless the scene is on: switching it off is
    allowed there).
    """
    return scene_device_count(record) > 0


def replay_request(record: Mapping[str, Any], stype: SceneType) -> dict[str, Any]:
    """Arguments of YorkApi.run_scene for a REPLAY scene, as the app builds them.

    xkqCtrlList: one {xkqCode, deviceType, xkqType, ctrlJson} per entry of
    the scene's xkqList, with the stored ctrlJson unchanged (a field the
    record lacks is left out, as JSON.stringify drops undefined).

    ctrlList (gateway indoor units, scenes tab runManualScenes): one
    {iuCtrlInfo, ouCtrlInfo: [], iezCode, deviceType} per gateway, holding
    {systemNo, iuSn, iuNo, iuType, ctrlJson} for each of its units in the
    scene; cat ("E") and ZHP ("6") units keep the stored ctrlJson, other
    gateways only its onoff, mode, temp, wind and humi. The app takes the
    gateways from the home detail; here they come from the units' own
    iezCode and deviceType, so units of a gateway that has left the home
    are still sent (the app would drop them).
    """
    xkqs = [entry for entry in _list(record.get("xkqList")) if isinstance(entry, Mapping)]
    return {
        "ctrl_type": stype.ctrl_type,
        "xkq_ctrl_list": [{key: entry[key] for key in _XKQ_KEYS if key in entry} for entry in xkqs],
        "ctrl_list": _iu_ctrl_list(record),
        "scene_id": record.get("smartId") if stype.sends_scene_id else None,
    }


def _iu_ctrl_list(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    gateways: dict[str, dict[str, Any]] = {}
    for unit in _list(record.get("iuList")):
        if not isinstance(unit, Mapping) or unit.get("iezCode") is None:
            continue  # the app matches units to gateways by iezCode
        code = unit["iezCode"]
        gateway = gateways.get(str(code))
        if gateway is None:
            gateway = gateways[str(code)] = {
                "iuCtrlInfo": [],
                "ouCtrlInfo": [],
                "iezCode": code,
                "deviceType": unit.get("deviceType"),
            }
        info = {key: unit[key] for key in _IU_KEYS if key in unit}
        ctrl_json = unit.get("ctrlJson")
        if str(unit.get("deviceType")) in _IU_RAW_CTRL_TYPES:
            if "ctrlJson" in unit:
                info["ctrlJson"] = ctrl_json
        else:
            info["ctrlJson"] = (
                {key: ctrl_json[key] for key in _IU_CTRL_KEYS if key in ctrl_json}
                if isinstance(ctrl_json, Mapping)
                else {}
            )
        gateway["iuCtrlInfo"].append(info)
    return list(gateways.values())


def result_outcome(code: str) -> bool | None:
    """One multiCtrlResult answer: True done, False failed, None still running."""
    if code == RESULT_SUCCESS:
        return True
    if code in RESULT_FAILURE:
        return False
    return None


def module_succeeded(data: Any) -> bool:
    """At the timeout the app counts the run a success if any moduleResult is "2"."""
    return any(
        isinstance(entry, Mapping) and str(entry.get("moduleResult")) == "2"
        for entry in _list(data)
    )
