"""Firmware updates (OTA) of the 约克智慧家 app's wire controllers.

HA-free rules for the firmware information the app shows. Verified in the
app bundle (es2015 chunks):

* Check: OtaService.requestOtas (a copy in chunks 11, 56, 57, 61, 70 and 71;
  main's httpUrl OTAS_GETOTAS "/api/appdevice/getOtas") posts
  ``{"homeIdList": [homeId]}``: the home page (chunk 11) when a home with
  devices loads (unless localStorage updateOtaFlag is set, see Install),
  the controller settings pages (56 xkq-color-set, 57 xkq-set) and the
  OTA page (61) when they open, always for the one home they show; the
  home page picks it from the answer by ``homeId == currentHomeId``, the
  other pages take ``homeList[0]``. How the cloud answers a request for
  several homes is unknown. ``data.homeList[]`` is ``{homeId, xkqList,
  deviceList}``, a wire controller's entry ``{xkqCode, otaFlg,
  statusProgress, otaList}`` and an update ``{otaId, otaType, confirmFlg,
  otaDesc, confirmTime, otaProcess, popupFlg}`` (the bundled sample
  assets/data/family-ota.json: otaId a number, the flags and types
  strings, confirmTime and statusProgress null).
* Offered: every page skips a controller unless its otaFlg is the string
  "0" (``"0"===n.otaFlg``); a controller installing an update reports
  otaFlg "1" in its status, for which the home page shows "OTA升级中..." and
  refuses control. A settings page shows "有新版本" when the controller's
  otaList has any entry, whatever its otaType or popupFlg (the standard
  page 57 matches the xkqCode, the colour page 56 forgets to); the home
  page opens the OTA page by itself only for entries with popupFlg "1".
* Shown: per controller the OTA page has the rows "线控器固件版本" (the
  controller record's versionMcu; an update icon for otaType "1"),
  "Wi-Fi版本" (versionWifi; otaType "5") and, only for otaType "2", one
  row per indoor unit of the record's iuList ("室内机软件版本", with an
  update icon for the units named in the entry's ouIuList; it shows the
  unit's ``version``, while the account's unit only carries romVersion
  "--"). The record is the home detail's, with the same versionMcu /
  versionWifi as the device list this integration reads, so the installed
  version here is those two. No new version number is shown for a wire
  controller: romVersionD is only read for gateways (deviceList), where it
  is shown in the place of the current version, so what it holds is
  unclear and it is not used here. An update's dialog ("版本升级") shows
  the otaDesc of the first entry of its type (release notes, lines
  separated by newlines).
* Never read by the app: confirmFlg, statusProgress (its shape is unknown:
  null in the sample). otaProcess and confirmTime are only copied into
  the install request.
* Install (not done by Home Assistant: updates are installed from the
  app; chunk 61): each way first checks that the controllers are online
  (xkqOnlineStatus; "线控器（…）升级失败，请保持设备在线" when one is not),
  then posts /api/appdevice/homes/{homeId}/otaCtrl (OTAS_OTACTRL)
  ``{homeId, ctrlFlg: "1", xkqList: [{xkqCode, otaList}], deviceList:
  []}``; code "200" means accepted. A row's update icon opens a dialog
  (版本升级: the otaDesc, 忽略 / 立刻更新) whose 立刻更新
  (OtaInfoComponent.updateOta) sends the controller's updates of the
  chosen otaType as ``{otaId, confirmTime, otaProcess}``. The page's
  bottom button (allUpdate) sends the full getOtas entries of the
  controllers it lists: labelled 一键更新 on the page the home page opens
  (xkqCode "all": every controller with a popupFlg "1" update), 立刻更新
  on the page a controller's settings page opens for that controller.
  After an accepted request the app sets localStorage updateOtaFlag and
  goes to the home page, which skips its check while the flag is set
  (cleared when the user opens a control page, runs a scene, opens the
  home management or comes back to the home tab). Leaving the OTA page
  the home page opened sends ctrlFlg "0" with the full entries (declines
  the popup). The progress request (OTAS_OTAPROGRESS,
  /api/appstatus/otaProgress) is never called.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .api import as_number

# otaType of the updates offered for a wire controller (the OTA page's
# xkqMcuOtaFlag / xkqWifiOtaFlag / xkqIuOtaFlag); gateways use others.
MCU = "1"
INDOOR_UNITS = "2"
WIFI = "5"
# Their names in the synthesized latest version, in the OTA page's row order.
COMPONENTS = {MCU: "MCU", WIFI: "Wi-Fi", INDOOR_UNITS: "IU"}
# The otaFlg of a controller whose updates the app offers.
NOT_UPDATING = "0"
# Home Assistant's limit for an update's release_summary.
SUMMARY_MAX = 255
# Version texts that mean "unknown" (the cloud's placeholder, e.g. an indoor
# unit's romVersion "--").
_UNKNOWN = frozenset({"", "--"})


def _text(value: Any) -> str:
    """A version, id or description as text ("" for null, booleans and containers)."""
    if value is None or isinstance(value, bool | Mapping | list):
        return ""
    return str(value).strip()


def same_id(left: Any, right: Any) -> bool:
    """JavaScript ``left == right`` for ids sent as numbers or strings.

    The home page picks its home from the answer with ``homeId ==
    currentHomeId``: 100001 and "100001" are the same home.
    """
    if left is None or right is None or isinstance(left, bool) or isinstance(right, bool):
        return False
    if str(left) == str(right):
        return True
    number = as_number(left)
    return number is not None and number == as_number(right)


def installed_version(record: Mapping[str, Any]) -> str | None:
    """MCU and Wi-Fi firmware of a controller record, as "C-01603 / W-00258".

    The OTA page's two rows (versionMcu, versionWifi). A missing or unknown
    ("--") part is left out; None without either.
    """
    parts = (_text(record.get(key)) for key in ("versionMcu", "versionWifi"))
    return " / ".join(part for part in parts if part not in _UNKNOWN) or None


def home_controllers(
    homes: Iterable[Any], home_id: Any, *, only_home: bool = True
) -> dict[str, dict[str, Any]] | None:
    """The controller entries of ``home_id`` in a getOtas answer, by xkqCode.

    A home with an empty or null xkqList has nothing offered (the
    account's answer lists its home with an empty xkqList), and so has a
    home missing from the answer to a request for it alone (``only_home``,
    what the app sends): the home page then finds nothing for it either.
    None, so that its controllers keep what was read before, when its
    xkqList is not a list at all, or when the home is missing from the
    answer to a request for several homes: the app never asks for more
    than one, so a missing home may just not have been answered.
    """
    for home in homes:
        if not isinstance(home, Mapping) or not same_id(home.get("homeId"), home_id):
            continue
        entries = home.get("xkqList")
        if entries is None:
            return {}
        if not isinstance(entries, list):
            return None
        return {
            str(entry["xkqCode"]): dict(entry)
            for entry in entries
            if isinstance(entry, Mapping) and entry.get("xkqCode")
        }
    return {} if only_home else None


def offered(entry: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The updates the app offers for one controller's getOtas entry.

    Only while the entry's otaFlg is the string "0", like every app page;
    then each update of its otaList, whatever its otaType or popupFlg. A
    null or malformed otaList offers nothing.
    """
    if not entry or entry.get("otaFlg") != NOT_UPDATING:
        return []
    updates = entry.get("otaList")
    if not isinstance(updates, list):
        return []
    return [update for update in updates if isinstance(update, Mapping)]


def _kind(update: Mapping[str, Any]) -> str:
    """otaType as text: the app compares strings, a number is read alike here."""
    return _text(update.get("otaType")) or "?"


def _label(kind: str) -> str:
    return COMPONENTS.get(kind, f"type {kind}")


def _grouped(updates: Iterable[Mapping[str, Any]]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Updates per otaType: MCU, Wi-Fi, indoor units (the OTA page's rows), then others."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for update in updates:
        groups.setdefault(_kind(update), []).append(update)
    order = [kind for kind in COMPONENTS if kind in groups]
    order += sorted(kind for kind in groups if kind not in COMPONENTS)
    return [(kind, groups[kind]) for kind in order]


def _id_order(oid: str) -> tuple[int, float, str]:
    """Sort key of otaIds: numbers by value (400007 before 400010), then texts."""
    number = as_number(oid)
    return (0, number, oid) if number is not None else (1, 0.0, oid)


def latest_version(installed: str | None, updates: Sequence[Mapping[str, Any]]) -> str | None:
    """The installed version while nothing is offered, else the offered updates.

    The answer has no new version number for a wire controller (see the
    module docstring), so the offered updates are named by their otaType
    and otaId, e.g. "MCU OTA 400004" or "MCU OTA 400004, Wi-Fi OTA 400005"
    (two updates of one type: "MCU OTA 400004+400007"). Never an
    installed version, so Home Assistant shows the update; the same as long
    as the same updates are offered, in whatever order the answer lists
    them (types in a fixed order, the ids of a type sorted), so a skipped
    update stays skipped; another one when a different update is offered.
    """
    if not updates:
        return installed
    names = []
    for kind, group in _grouped(updates):
        ids = sorted(
            {oid for update in group if (oid := _text(update.get("otaId")))}, key=_id_order
        )
        name = f"{_label(kind)} OTA"
        names.append(f"{name} {'+'.join(ids)}" if ids else name)
    return ", ".join(names)


def release_summary(updates: Sequence[Mapping[str, Any]]) -> str | None:
    """Release notes (otaDesc) of the offered updates, cut to 255 characters.

    Per otaType the first non-empty otaDesc (the app's dialog shows the
    first entry of the chosen type); when more than one type is offered,
    each starts with its name ("MCU: …"). None without any.
    """
    groups = _grouped(updates)
    texts = []
    for kind, group in groups:
        desc = next((text for update in group if (text := _text(update.get("otaDesc")))), "")
        if desc:
            texts.append(f"{_label(kind)}: {desc}" if len(groups) > 1 else desc)
    if not texts:
        return None
    summary = "\n".join(texts)
    return summary if len(summary) <= SUMMARY_MAX else f"{summary[: SUMMARY_MAX - 1]}…"
