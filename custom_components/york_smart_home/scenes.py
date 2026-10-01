"""Cloud scenes (智能场景) of the 约克智慧家 app.

HA-free rules for the scenes the app's scenes tab offers: which scenes it
shows, the control it shows for each scene type (a run button, an on/off
toggle or nothing), how their state is read, what it refuses, and the
request that runs a stored scene. Verified in the app bundle (es2015
chunks):

* List: the scenes tab (ScenesPage, chunk 15) reads the home detail
  (FAMILY_DETAIL, GET /api/apphome/homes/{homeId}) in ionViewWillEnter, then
  its own list (SCENE_GET_ALL_SCENE, GET .../apptimer/homes/{homeId}/smarts,
  ``data.smartList``) in initPageData. The home page (chunk 11) runs and
  switches scenes from the detail's ``homeDetail.smartList``, which holds
  the same records (it drops favoriteFlg "0" from its display itself). HA
  reads the detail only: the records and the feature flags at once
  (YorkApi.get_scenes).
* Shown: filterMyScenes keeps types 8, 9, M, O (and 3, 4, A, Z, which the
  template never renders) for the manual and timer lists and B–E for the
  linkage list; every other type reaches the screen only through
  initRecommendSceneFakeData, which keeps an existing 1, 2 or 5 always, a
  6 only while the detail's leaveEnergyFlg is truthy, 7 while nightQuiteFlg
  is, F while "1" == kszrFlg and G–L while "1" == sceneGFlg..sceneLFlg (the
  home page filters its favourites the same way, and also 1/2 by scene2Flg,
  5 by scene5Flg and M by sceneMFlg). N is shown nowhere. A scene the app
  hides gets no entity (scene_offered).
* Controls: the tab's template (chunk 18) gives types 1, 2, F, G, I and J a
  run (play) button (J's button switches its openFlg), every other
  recommended type and the timer list (types 9, M, O) a toggle that is "on"
  only for openFlg "1", the linkage list (B–E) a toggle that is "on" only
  for actionFlg "1", the pets scene (H) a toggle that is "on" for actionFlg
  "1" or "2" (HANGUP), and manual scenes (8) a run button.
* Run: there is no "execute scene" call (SCENE_EXECUTE /cmds/sceneControl
  is never used). The app replays the stored commands through
  multiIuOuCtrl (runManualScenes / preControlSceneParams), or posts
  greenhouse and party scenes once to .../scene/type/{type}/{sceneId}.
* Toggle: PUT .../smarts/{smartId}/openFlg {openFlg, updateTime}
  (openCloseScenes); vacation, two-supply dehumidification and motion
  scenes POST .../scene/type/{type}/{sceneId} with forceOpenFlg "0"
  (recommendedSceneExecution); linkage and pets scenes PUT
  .../newScenes/{smartId}/flag/0 with the new actionFlg (runAutoScenes).

Of the other flags, scene2Flg also decides the leave / return home run's
ctrlType (run_ctrl_type); smartSceneFlg only concerns the ZHP gateways'
own smart scenes, and smartFlg1/2 and sceneNFlg are never read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
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
    # multiIuOuCtrl ctrlType of a REPLAY run from the scenes tab.
    ctrl_type: str | None = None
    # The home page's ctrlType for the same run, sent when the home page
    # shows the scene (leave / return home: run_ctrl_type).
    home_ctrl_type: str | None = None
    # A REPLAY run also sends sceneId (AI water/fluorine fast heat only).
    sends_scene_id: bool = False
    # The home detail flag the scenes tab shows the type by (scene_offered).
    feature: str | None = None
    # Linkage and pets scenes: the record field naming their air box
    # (约克小方), without which the app does not switch them on.
    sensor_key: str | None = None


SCENE_TYPES: dict[str, SceneType] = {
    # Leave / return home: the scenes tab replays them with YO438 like a
    # manual scene, the home page's buttons with their own ctrlTypes
    # (SCENE_CONTROL_LEAVE_HOME / _GO_HOME), so the cloud sees which one ran.
    "1": SceneType("leave_home", "离家", RUN, REPLAY, "YO438", "YO431"),
    "2": SceneType("go_home", "回家", RUN, REPLAY, "YO438", "YO432"),
    "5": SceneType("sleep_curve", "睡眠曲线", SWITCH, OPEN_FLAG),
    "6": SceneType(
        "leave_home_energy_saving", "离家节能", SWITCH, OPEN_FLAG, feature="leaveEnergyFlg"
    ),
    "7": SceneType("night_mute", "夜间静音", SWITCH, OPEN_FLAG, feature="nightQuiteFlg"),
    "8": SceneType("manual", "手动场景", RUN, REPLAY, "YO438"),
    # The legacy timer: listed with a toggle, editing it only says
    # "定时场景已升级".
    "9": SceneType("timer_legacy", "定时场景", SWITCH, OPEN_FLAG),
    # runAutoScenes checks boxCode for summer, winter, dehumidification and
    # pets, boxCodes (its else branch) for purification.
    "B": SceneType("summer", "夏季防暑热场景", SWITCH, AUTO, sensor_key="boxCode"),
    "C": SceneType("winter", "冬季防过冷场景", SWITCH, AUTO, sensor_key="boxCode"),
    "D": SceneType("dehumidify", "除湿智能场景", SWITCH, AUTO, sensor_key="boxCode"),
    "E": SceneType("purify", "净化智能场景", SWITCH, AUTO, sensor_key="boxCodes"),
    "F": SceneType(
        "ai_fast_heat", "Ai水氟速热", RUN, REPLAY, "YO43F", sends_scene_id=True, feature="kszrFlg"
    ),
    "G": SceneType("advance_heating", "提前暖房", RUN, RECOMMENDED, feature="sceneGFlg"),
    "H": SceneType("pets", "宠物场景", SWITCH, AUTO, feature="sceneHFlg", sensor_key="boxCode"),
    "I": SceneType("party", "聚会场景", RUN, RECOMMENDED, feature="sceneIFlg"),
    # Motion: a run button in the app, which switches openFlg and shows
    # "（执行中）" while it is "1": an on/off state, so a switch here.
    "J": SceneType("motion", "运动场景", SWITCH, RECOMMENDED, feature="sceneJFlg"),
    "K": SceneType("vacation", "度假场景", SWITCH, RECOMMENDED, feature="sceneKFlg"),
    "L": SceneType("smart_dehumidify", "两联供智慧除湿", SWITCH, RECOMMENDED, feature="sceneLFlg"),
    "M": SceneType("hot_water_timer", "热水定时场景", SWITCH, OPEN_FLAG),
    "O": SceneType("timer", "定时场景", SWITCH, OPEN_FLAG),
}
# Tested for truthiness (booleans in the home detail: `homeDetailInfo.
# leaveEnergyFlg && ...`); every other feature flag with `"1" == flag`.
_BOOLEAN_FEATURES = frozenset({"leaveEnergyFlg", "nightQuiteFlg"})

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


def js_truthy(value: Any) -> bool:
    """JavaScript truthiness of a JSON value: "0" is truthy, 0 and "" are not."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, int | float):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return True


def js_equals(value: Any, text: str) -> bool:
    """JavaScript ``text == value`` for a numeric string such as "1".

    A string compares as a string, a number or boolean by its number
    ("1" == 1 and "1" == true hold); null and objects never match.
    """
    if isinstance(value, str):
        return value == text
    if isinstance(value, bool | int | float):
        return float(value) == float(text)
    return False


def scene_type(record: Mapping[str, Any]) -> SceneType | None:
    """The type's rules, or None for types the app does not list."""
    return SCENE_TYPES.get(str(record.get("type")))


def scene_offered(record: Mapping[str, Any], flags: Mapping[str, Any]) -> bool:
    """Whether the app's scenes tab shows the scene, given the home's flags.

    A type the app lists, and for the types tied to a feature, the feature
    switched on for the home (a missing flag hides them, as in the app).
    """
    stype = scene_type(record)
    if stype is None:
        return False
    if stype.feature is None:
        return True
    value = flags.get(stype.feature)
    if stype.feature in _BOOLEAN_FEATURES:
        return js_truthy(value)
    return js_equals(value, "1")


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
    """On/off of the switch: the app's toggle, with HANGUP as running.

    openFlg: only "1", as every openFlg toggle of the app (HANGUP shows as
    off). actionFlg: "1" and "2" (HANGUP). The pets toggle shows both as on
    and its editor refuses both with "场景正在执行中"; the linkage list's
    toggle (B–E) lights up for "1" only, but runAutoScenes treats "2" as
    running too: a tap sends "0" for both. HA follows the tap, so a hung-up
    linkage scene is on here (attribute ``status`` "hangup") although the
    app's toggle shows it as off.
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

    Scenes tab: "请先点击“场景卡片”添加空调" (runManualScenes), "请先编辑该
    场景并添加空调" (openCloseScenes, in both directions) and "请先添加设备"
    (runAutoScenes, switching on only); home page: "先去场景下添加一台空调/
    设备吧。" (runScene, which lets a running scene of most types be
    switched off). HA only refuses running and switching on.
    """
    return scene_device_count(record) > 0


def has_sensor(record: Mapping[str, Any], stype: SceneType) -> bool:
    """A linkage or pets scene also needs its air box (约克小方) to switch on.

    Scenes tab (runAutoScenes, from actionFlg "0"): "请先添加设备" without a
    boxCode (B, C, D, H) or boxCodes (E); home page (runScene): "先去场景下
    添加一台约克小方吧。" for a scene whose boxList is there but empty.
    Other types have no air box.
    """
    if stype.sensor_key is None:
        return True
    box_list = record.get("boxList")
    if isinstance(box_list, list) and not box_list:
        return False
    return js_truthy(record.get(stype.sensor_key))


def run_ctrl_type(
    record: Mapping[str, Any], stype: SceneType, flags: Mapping[str, Any]
) -> str | None:
    """multiIuOuCtrl ctrlType of a replayed run, as the app page offering it sends it.

    Leave / return home are buttons on the home page only for its
    favourites (it drops `"0" == favoriteFlg`) and only while "1" ==
    scene2Flg (displaySmartList); those send YO431 / YO432
    (preControlSceneParams). Otherwise only the scenes tab runs them, with
    YO438 like a manual scene (runManualScenes).
    """
    if (
        stype.home_ctrl_type is not None
        and js_equals(flags.get("scene2Flg"), "1")
        and not js_equals(record.get("favoriteFlg"), "0")
    ):
        return stype.home_ctrl_type
    return stype.ctrl_type


def replay_request(
    record: Mapping[str, Any], stype: SceneType, flags: Mapping[str, Any]
) -> dict[str, Any]:
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
        "ctrl_type": run_ctrl_type(record, stype, flags),
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
