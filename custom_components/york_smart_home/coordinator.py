"""Data update coordinator for York Smart Home.

Polling and control follow the 约克智慧家 app: status is polled
incrementally with the last report's roundTime (home page), and each change
is diffed against the current state and sent right away with only the
fields that differ (xkq-color-control page, without its 1.5s debounce).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HomeScenes,
    YorkApi,
    YorkAuthError,
    YorkClientOutdatedError,
    YorkCommandError,
    YorkError,
    YorkRateLimitError,
    YorkSceneConflictError,
    YorkSessionConflictError,
    YorkSessionTakenError,
    as_number,
    as_timestamp,
    is_on,
    parse_constants,
)
from .const import (
    CLIENT_OUTDATED_PAUSE,
    COMMAND_GRACE,
    CONF_SCAN_INTERVAL,
    CONF_SESSION_YIELD_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SESSION_YIELD_MINUTES,
    DISCOVERY_INTERVAL,
    DOMAIN,
    FAILURES_BEFORE_UNAVAILABLE,
    FAN_MODES,
    FILTER_TIPS_KEYS,
    FUNC_LABELS,
    ISSUE_CLIENT_OUTDATED,
    ISSUE_SESSION_TAKEN,
    KEY_OTA,
    KEY_POWER,
    KEY_ROUND_TIME,
    KEY_TIMER_TIME,
    KEY_TIMESTAMP,
    MODES,
    ONLINE_INTERVAL,
    OTA_FIRST_DELAY,
    OTA_INTERVAL,
    OTA_RECHECK_DELAY,
    OTA_RETRY_INTERVAL,
    REFRESH_DELAY_AFTER_CONTROL,
    RELOGIN_BACKOFF_MAX,
    RELOGIN_BACKOFF_MIN,
    SCENE_INTERVAL,
    SCENE_READ_TIMEOUT,
    SCENE_RESULT_INTERVAL,
    SCENE_RESULT_POLLS,
    SCENE_RESULT_TIMEOUT,
    SETTINGS_KEYS,
    TIMER_CHECK_DELAY,
    TIMER_HOURS,
    TIMER_KEYS,
    TIMER_PIN_TOLERANCE_MS,
)
from .control import (
    CommandRefused,
    build_command,
    command_state,
    new_timer_turns_on,
    timer_active,
    timer_cancel_state,
    timer_end_ms,
    timer_set_state,
)
from .firmware import home_controllers, installed_version
from .profile import Profile, profile_for
from .scenes import (
    CLOSE,
    OPEN,
    OPEN_FLAG,
    RECOMMENDED,
    REPLAY,
    RUN,
    SWITCH,
    SceneType,
    flag_key,
    has_devices,
    has_sensor,
    module_succeeded,
    replay_request,
    result_outcome,
    scene_name,
    scene_offered,
    scene_target_reached,
    scene_type,
)
from .status import cooling_down

_LOGGER = logging.getLogger(__name__)

type YorkConfigEntry = ConfigEntry[YorkCoordinator]

# Live settings the app never reads from constantJson (the timer fields
# come from xkqStatusListGjy[0] too).
_STATUS_ONLY_KEYS = frozenset((*SETTINGS_KEYS, *FILTER_TIPS_KEYS, *TIMER_KEYS))

# Refusals that depend only on the controller's capabilities (constants),
# not on its live state: safe to check against a cached state that may be
# minutes old while the session is left to the app.
_CAPABILITY_REFUSALS = frozenset(
    {"function_not_supported", "swing_not_supported", "unsupported_option"}
)

# Groups of mutually exclusive flags: selecting one clears the others.
_EXCLUSIVE_GROUPS = [
    [key for key, _ in MODES.values()],
    [key for key, _ in FAN_MODES.values()],
]


def report_cursor(report: dict[str, Any]) -> int:
    """The app sends a report's roundTime back as the next cursor.

    Falls back to the report timestamp; both may arrive as strings.
    """
    return as_timestamp(report.get(KEY_ROUND_TIME)) or as_timestamp(report.get(KEY_TIMESTAMP))


def issue_id(entry: ConfigEntry, key: str) -> str:
    return f"{key}_{entry.entry_id}"


@dataclass
class YorkDevice:
    """A wire controller and its latest state."""

    info: dict[str, Any]
    # Live state from the status reports.
    status: dict[str, Any] = field(default_factory=dict)
    # constantJson of the controller detail: capabilities, locks, limits.
    constants: dict[str, Any] = field(default_factory=dict)
    online: bool = True
    # False once the controller disappears from the account.
    present: bool = True
    # Fields just set by a command: key -> (value, monotonic deadline).
    expected: dict[str, tuple[Any, float]] = field(default_factory=dict)
    # Polling cursor: roundTime (else timestamp) of the newest report merged.
    cursor: int = 0
    # Monotonic time after which the next poll reads this controller's full
    # state (cursor 0) once, e.g. to check a filter reset after its grace.
    recheck_after: float | None = None
    # Last timer set from HA: (timerSimpleTime of the answer, hours), so the
    # timer select can show its duration (the status only has the end). An
    # answer without a time is pinned to the first end reported after the
    # grace period.
    timer_choice: tuple[int | None, float] | None = None
    # The timer end (epoch ms) a full read is scheduled after, and when
    # (monotonic): once per end, TIMER_CHECK_DELAY after it.
    timer_check_end: int | None = None
    timer_check_after: float | None = None
    # The controller's entry in the last firmware answer (getOtas: otaFlg,
    # otaList ...); {} when the answer listed none for it (nothing offered),
    # None until its home was read.
    firmware: dict[str, Any] | None = None

    @property
    def code(self) -> str:
        return self.info["xkqCode"]

    @property
    def home_id(self) -> int:
        return self.info["homeId"]

    @property
    def device_type(self) -> str:
        return str(self.info.get("type") or "C")

    @property
    def xkq_type(self) -> str:
        return str(self.info.get("xkqType") or "9")

    @property
    def view(self) -> dict[str, Any]:
        """Status with the constants laid over it.

        Like the app's control page, capability, lock and limit keys come
        from the constants whenever they contain them; everything else is
        the live status. The settings page reads its keys only from the live
        status (xkqStatusListGjy[0]); they and the live filter tips are never
        taken from the constants.
        """
        return {
            **self.status,
            **{
                k: v
                for k, v in self.constants.items()
                if _has_value(v) and k not in _STATUS_ONLY_KEYS
            },
        }

    @property
    def profile(self) -> Profile:
        """The rules of the app page this controller opens (colour / standard)."""
        return profile_for(self.xkq_type)

    @property
    def ota(self) -> bool:
        """The controller is installing a firmware update (loose "1" == 1)."""
        return is_on(self.status.get(KEY_OTA))

    @property
    def cooling_down(self) -> bool:
        """The system cools down after self cleaning (self_clean == 2)."""
        return cooling_down(self.status)


@dataclass
class YorkScene:
    """A cloud scene (智能场景) of a home: the record of the app's scene list."""

    home_id: int
    record: dict[str, Any]
    # False once the scene is no longer in the home's list (deleted in the app).
    present: bool = True
    # Whether the app shows it: a type it lists, with its feature switched
    # on for the home (scenes.scene_offered). A hidden scene gets no entity
    # (an existing one becomes unavailable).
    offered: bool = True

    @property
    def smart_id(self) -> str:
        return str(self.record.get("smartId"))

    @property
    def stype(self) -> SceneType | None:
        """How the app offers the scene's type (None: the app does not list it)."""
        return scene_type(self.record)

    @property
    def name(self) -> str:
        return scene_name(self.record)

    @property
    def request_home_id(self) -> Any:
        """homeId for the scene's requests: the record's own, as the app sends it."""
        home_id = self.record.get("homeId")
        return self.home_id if home_id is None else home_id


@dataclass
class YorkHome:
    """A home (家庭) of the account with a supported wire controller, and its scenes."""

    home_id: int
    name: str | None = None
    scenes: dict[str, YorkScene] = field(default_factory=dict)
    # Feature flags of the home detail (SCENE_FLAG_KEYS), as last read.
    flags: dict[str, Any] = field(default_factory=dict)
    # The scene list has been read at least once.
    loaded: bool = False
    # False once no supported wire controller is left in the home: its
    # scenes are no longer read (and their entities become unavailable).
    present: bool = True


class YorkCoordinator(DataUpdateCoordinator[dict[str, YorkDevice]]):
    """Polls every wire controller on the account."""

    config_entry: YorkConfigEntry

    def __init__(self, hass: HomeAssistant, entry: YorkConfigEntry, api: YorkApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.api = api
        self.devices: dict[str, YorkDevice] = {}
        self._last_discovery: float | None = None
        self._last_online: float | None = None
        self._full_status = True
        self._locks: dict[str, asyncio.Lock] = {}
        self._unsub_control_refresh: Callable[[], None] | None = None
        self._paused_until: float | None = None
        self._failures = 0
        # (Names chosen not to clash with DataUpdateCoordinator internals.)
        # Set while the session is left to another device; polling logs in
        # again once this monotonic time has passed.
        self._yielded_until: float | None = None
        self._yield_started: float | None = None
        # Growing delay between failed (non-auth) logins after a yield.
        self._relogin_backoff = 0
        # Set while the cloud demands a newer app version (code 300).
        self._outdated_until: float | None = None
        # Cloud scenes per homeId, read in a status poll every SCENE_INTERVAL.
        self.homes: dict[int, YorkHome] = {}
        # Monotonic time of the last scene list read (None: read at the next poll).
        self._last_scene_fetch: float | None = None
        # Error of the last failed scene list read, logged once as a warning.
        self._scene_read_error: str | None = None
        self._scene_locks: dict[int, asyncio.Lock] = {}
        # Scene actions are counted each time one takes its home's fresh
        # list and each time its request is answered: the count so far, and
        # per homeId the count at its latest. A list read in a poll that
        # overlaps one of them may predate it and is dropped (_fetch_scenes).
        self._scene_actions = 0
        self._scene_action_at: dict[int, int] = {}
        # Firmware information (getOtas), read on its own slow timer: the
        # armed timer and its due time (loop time), whether a read is in
        # progress, and the error of the last failed read (logged once as a
        # warning).
        self._unsub_firmware_timer: Callable[[], None] | None = None
        # Like the polling timer, cancelled when Home Assistant stops.
        self._firmware_job = HassJob(
            self._firmware_timer_fired, f"{DOMAIN} firmware timer", cancel_on_shutdown=True
        )
        self._firmware_due: float | None = None
        self._firmware_reading = False
        self._firmware_read_error: str | None = None
        # Read again soon once the read in progress is done.
        self._firmware_again = False
        # A controller finished installing an update: rediscover (installed
        # versions, constants) after the next firmware read.
        self._firmware_rediscover = False
        entry.async_on_unload(self._cancel_control_refresh)
        entry.async_on_unload(self._cancel_firmware_timer)

    @property
    def session_yielded(self) -> bool:
        return self._yielded_until is not None

    @property
    def client_outdated(self) -> bool:
        return self._outdated_until is not None

    @property
    def scene_error(self) -> str | None:
        """Why the last scene list read failed (None: it worked)."""
        return self._scene_read_error

    @property
    def firmware_error(self) -> str | None:
        """Why the last firmware read failed (None: it worked, or none was tried)."""
        return self._firmware_read_error

    # --------------------------------------------------------------- polling

    async def _async_update_data(self) -> dict[str, YorkDevice]:
        try:
            await self._poll()
        except UpdateFailed as err:
            if self.data is None and (
                self._yielded_until is not None or self._outdated_until is not None
            ):
                # Set up while another device holds the session, or while the
                # cloud demands a newer app: load without devices rather than
                # fail. Every setup retry would start over with a new
                # coordinator (logging the app out, or ignoring the hourly
                # pause). The devices appear once the cloud answers again.
                return self.devices
            self._failures += 1
            if self.data is not None and self._failures < FAILURES_BEFORE_UNAVAILABLE:
                # Transient: keep the last known state for now.
                _LOGGER.debug(
                    "York poll failed (%s of %s tolerated): %s",
                    self._failures,
                    FAILURES_BEFORE_UNAVAILABLE - 1,
                    err,
                )
                return self.devices
            raise
        self._failures = 0
        return self.devices

    async def _poll(self, *, scenes: bool = True) -> None:
        """One polling round; ``scenes`` also reads the scene lists when due."""
        now = time.monotonic()
        # Paused states fail fast, without contacting the cloud.
        if self._paused_until is not None and now < self._paused_until:
            raise UpdateFailed("York cloud asked to pause polling (HTTP 429)")
        self._paused_until = None
        if self._outdated_until is not None and now < self._outdated_until:
            raise UpdateFailed("York cloud requires a newer app version; polling paused")
        if self._yielded_until is not None and now < self._yielded_until:
            raise UpdateFailed("York account is in use on another device; not polling")
        try:
            if self._yielded_until is not None:
                # The yield period is over: take the session back.
                try:
                    await self.api.relogin()
                except (YorkAuthError, YorkClientOutdatedError, YorkRateLimitError):
                    raise  # handled below; the yield stays pending
                except YorkError:
                    # Cloud trouble: do not send a login on every poll.
                    self._relogin_backoff = min(
                        max(self._relogin_backoff * 2, RELOGIN_BACKOFF_MIN), RELOGIN_BACKOFF_MAX
                    )
                    self._yielded_until = now + self._relogin_backoff
                    raise
                self._end_yield()

            if self._last_discovery is None or now - self._last_discovery >= DISCOVERY_INTERVAL:
                await self._discover()
                self._last_discovery = now
                self._full_status = True

            active = [d for d in self.devices.values() if d.present]
            by_home: dict[int, list[YorkDevice]] = {}
            for dev in active:
                by_home.setdefault(dev.home_id, []).append(dev)
            for home_id, devs in by_home.items():
                # Home page: a controller in OTA is always queried in full.
                full = {
                    d.code
                    for d in devs
                    if self._full_status
                    or d.ota
                    or (d.recheck_after is not None and now >= d.recheck_after)
                    or (d.timer_check_after is not None and now >= d.timer_check_after)
                }
                statuses = await self.api.get_status(
                    home_id, [(d.code, 0 if d.code in full else d.cursor) for d in devs]
                )
                for dev in devs:
                    if (status := statuses.get(dev.code)) is not None:
                        if dev.code in full:
                            # Before merging: the merge may schedule a check
                            # for a new timer end.
                            dev.recheck_after = None
                            if dev.timer_check_after is not None and now >= dev.timer_check_after:
                                dev.timer_check_after = None
                        self._merge(dev, status)
            self._full_status = False

            if active and (self._last_online is None or now - self._last_online >= ONLINE_INTERVAL):
                online = await self.api.get_online([(d.code, d.device_type) for d in active])
                for dev in active:
                    # A controller missing from the answer is not known to be online.
                    dev.online = online.get(dev.code, False)
                self._last_online = now

            if scenes and self._scenes_due(now):
                actions = self._scene_actions
                await self._fetch_scenes()
                if self._scene_actions == actions:
                    # Unless a scene action took its home's list or got its
                    # answer meanwhile: then that home's answer was dropped,
                    # and the read after the action's request
                    # (_after_scene_action) is still to come.
                    self._last_scene_fetch = now
        except YorkSessionTakenError as err:
            self._start_yield(now)
            raise UpdateFailed(str(err)) from None
        except YorkSessionConflictError as err:
            _LOGGER.warning(
                "York session was revoked again right after logging in; the account "
                "is probably also used by the app. Not logging in again automatically"
            )
            raise ConfigEntryAuthFailed(str(err)) from None
        except YorkAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from None
        except YorkClientOutdatedError as err:
            self._start_outdated(now, err)
            raise UpdateFailed(str(err)) from None
        except YorkRateLimitError as err:
            self._paused_until = now + err.retry_after
            raise UpdateFailed(str(err)) from None
        except YorkError as err:
            raise UpdateFailed(str(err)) from None
        if self._outdated_until is not None:
            _LOGGER.info("York cloud accepts requests again")
            self._outdated_until = None
        # Unconditionally: the issue may be left over from an earlier
        # coordinator (deleting a missing issue does nothing).
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.config_entry, ISSUE_CLIENT_OUTDATED))

    async def _discover(self) -> None:
        records = {r["xkqCode"]: r for r in await self.api.get_wire_controllers()}
        # A new installed version (an update was installed) or a new
        # controller: read the offered firmware updates again soon. Armed
        # right away: the next discovery sees no change any more, so an
        # error further down (e.g. a 429 reading the constants) must not
        # lose it.
        for code, dev in self.devices.items():
            dev.present = code in records
            if dev.present:
                old = installed_version(dev.info)
                dev.info = records[code]
                if installed_version(dev.info) != old:
                    _LOGGER.debug("York %s firmware is now %s", code, installed_version(dev.info))
                    self._show_versions(dev)
                    self._firmware_soon()
        for code, record in records.items():
            if code not in self.devices:
                _LOGGER.debug("Discovered wire controller %s", code)
                self.devices[code] = YorkDevice(info=record)
                self._firmware_soon()
        for dev in self.devices.values():
            if dev.present:
                dev.constants = await self._fetch_constants(dev)
        # Re-check connectivity right after (re)discovery.
        self._last_online = None

    async def _fetch_constants(self, dev: YorkDevice) -> dict[str, Any]:
        """Constants from the controller detail, as the app's control page reads them.

        Falls back to the deviceList record's copy if the detail request fails.
        """
        try:
            constants = await self.api.get_wire_controller_detail(dev.home_id, dev.code)
        except (YorkAuthError, YorkClientOutdatedError, YorkRateLimitError):
            raise
        except YorkError as err:
            _LOGGER.debug("York %s: detail unavailable (%s), using deviceList", dev.code, err)
            constants = {}
        return constants or parse_constants(dev.info.get("constantJson"))

    @callback
    def _show_versions(self, dev: YorkDevice) -> None:
        """Show a controller's new firmware versions on its device as well.

        The device takes them from the controller record when its entities
        are set up (sw_version versionMcu, hw_version versionWifi); after an
        update they would only change at the next restart.
        """
        registry = dr.async_get(self.hass)
        # (async_get_device is deprecated in newer releases, and its
        # replacement does not exist in 2025.3.)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if (DOMAIN, dev.code) in device.identifiers:
                registry.async_update_device(
                    device.id,
                    sw_version=dev.info.get("versionMcu"),
                    hw_version=dev.info.get("versionWifi"),
                )

    def _merge(self, dev: YorkDevice, status: dict[str, Any]) -> None:
        """Merge a status report (_merge_status); notice a finished firmware update.

        When a controller stops reporting otaFlg "1" (the app's "OTA升级中..."),
        its offered updates are read again soon, and its installed versions
        and constants after that read (rediscovery).
        """
        updating = dev.ota
        _merge_status(dev, status)
        if updating and not dev.ota:
            _LOGGER.debug("York %s finished installing a firmware update", dev.code)
            self._firmware_rediscover = True
            self._firmware_soon()

    # ---------------------------------------------------------------- scenes

    def _scene_homes(self) -> dict[int, Any]:
        """Homes with a supported wire controller: homeId -> homeName.

        The scenes tab reads the list of any home with a device; homes
        without a controller this integration supports are left out.
        """
        return {d.home_id: d.info.get("homeName") for d in self.devices.values() if d.present}

    def _scenes_due(self, now: float) -> bool:
        """Read the scene lists every SCENE_INTERVAL, or now for a new or removed home."""
        if self._last_scene_fetch is None or now - self._last_scene_fetch >= SCENE_INTERVAL:
            return True
        known = {home_id for home_id, home in self.homes.items() if home.present}
        return known != set(self._scene_homes())

    async def _fetch_scenes(self) -> None:
        """Read the scene list of every home, one after another.

        Errors of the session as a whole (taken by another device, app
        update demanded, rate limit) propagate like those of the status
        poll. Any other error keeps the last list, so the climate poll goes
        on; the read is retried at the next slow tick. That includes a 403
        (an optional read, see YorkApi.get_scenes) and a read that takes
        longer than SCENE_READ_TIMEOUT.

        A scene action does not wait for this read (nor this read for an
        action). An answer that arrives after an action of its home took
        the home's fresh list or got its request's answer may predate
        them: it is dropped rather than laid over the newer state, and the
        action's own read and the one after its request bring the list.
        """
        homes = self._scene_homes()
        for home_id, home in self.homes.items():
            home.present = home_id in homes
        failed = False
        for home_id, name in homes.items():
            home = self.homes.setdefault(home_id, YorkHome(home_id))
            home.present = True
            if name:
                home.name = str(name)
            actions = self._scene_actions
            try:
                async with asyncio.timeout(SCENE_READ_TIMEOUT):
                    result = await self.api.get_scenes(home_id)
            except (
                YorkSessionTakenError,
                YorkSessionConflictError,
                YorkClientOutdatedError,
                YorkRateLimitError,
            ):
                raise
            except (YorkError, TimeoutError) as err:
                failed = True
                reason = str(err) or f"no answer within {SCENE_READ_TIMEOUT} s"
                if self._scene_read_error is None:
                    _LOGGER.warning(
                        "Could not read the York scenes (%s); keeping the last ones and "
                        "trying again in %s minutes",
                        reason,
                        SCENE_INTERVAL // 60,
                    )
                else:
                    _LOGGER.debug("York scenes still unreadable: %s", reason)
                self._scene_read_error = reason
                continue
            if self._scene_action_at.get(home_id, 0) > actions:
                _LOGGER.debug("York scene list read during a scene action of its home: dropped")
                continue
            _merge_scenes(home, result)
        if not failed and self._scene_read_error is not None:
            _LOGGER.info("York scenes can be read again")
            self._scene_read_error = None

    def _scene(self, home_id: int, smart_id: str, control: str) -> YorkScene:
        """The scene an entity with the app's ``control`` (RUN / SWITCH) acts on.

        From the list as last read: gone if it was deleted in the app (or is
        no longer of a type with that control); refused like the app if the
        app does not show it in this home.
        """
        home = self.homes.get(home_id)
        scene = home.scenes.get(smart_id) if home is not None else None
        if (
            home is None
            or scene is None
            or not home.present
            or not scene.present
            or scene.stype is None
            or scene.stype.control != control
        ):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="scene_gone")
        if not scene.offered:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="scene_hidden",
                translation_placeholders={"scene": scene.name},
            )
        return scene

    # -------------------------------------------------------------- firmware

    @callback
    def async_start_firmware_checks(self) -> None:
        """Start the firmware timer: a first read OTA_FIRST_DELAY after setup.

        Then every OTA_INTERVAL. Apart from the status polling and from
        setup: a read never delays or fails a poll, and no poll waits for it.
        """
        self._arm_firmware_timer(OTA_FIRST_DELAY)

    @callback
    def _arm_firmware_timer(self, delay: float) -> None:
        self._cancel_firmware_timer()
        self._firmware_due = self.hass.loop.time() + delay
        self._unsub_firmware_timer = async_call_later(self.hass, delay, self._firmware_job)

    @callback
    def _cancel_firmware_timer(self) -> None:
        if self._unsub_firmware_timer is not None:
            self._unsub_firmware_timer()
            self._unsub_firmware_timer = None
        self._firmware_due = None

    @callback
    def _firmware_timer_fired(self, _now: datetime) -> None:
        self._unsub_firmware_timer = None
        self._firmware_due = None
        # Cancelled when the entry unloads (after the timer is).
        self.config_entry.async_create_background_task(
            self.hass, self._firmware_read(), f"{DOMAIN} firmware check"
        )

    async def _firmware_read(self) -> None:
        """One read on the firmware timer, then the timer for the next one.

        A cancelled read (the entry unloads) arms nothing.
        """
        self._firmware_reading = True
        try:
            delay = await self._fetch_firmware()
        except Exception:  # never let a bug stop the slow timer for good
            _LOGGER.exception("Unexpected error reading the York firmware information")
            delay = OTA_RETRY_INTERVAL
        self._firmware_reading = False
        if self._firmware_again:
            self._firmware_again = False
            delay = min(delay, OTA_RECHECK_DELAY)
        self._arm_firmware_timer(delay)

    @callback
    def _firmware_soon(self) -> None:
        """Read the firmware information OTA_RECHECK_DELAY from now (or sooner if due).

        After a controller finished installing an update, its installed
        version changed or a new controller appeared. Does nothing before
        the timer is started: the first read comes shortly after setup.
        """
        if self._firmware_reading:
            self._firmware_again = True
        elif (
            self._firmware_due is not None
            and self._firmware_due > self.hass.loop.time() + OTA_RECHECK_DELAY
        ):
            self._arm_firmware_timer(OTA_RECHECK_DELAY)

    async def _fetch_firmware(self) -> float:
        """Read the offered firmware updates of every home; returns the delay to the next read.

        One getOtas request for all homes with a supported controller (the
        app asks for the home it shows); a home missing from an answer for
        several homes keeps what was read before (home_controllers). Put
        off by OTA_RETRY_INTERVAL, without a request, while polling is
        switched off in the entry's system options ("Enable polling for
        changes", which also stops the status polling, and reloads the
        entry when changed), there is no controller yet or polling is on
        hold (the session is left to another device, the cloud demands a
        newer app or asked to pause, or the polls fail).

        A controller installing an update (otaFlg "1" in its status) keeps
        the entry read before, so HA goes on showing the update it offered
        as being installed: what the cloud lists for it meanwhile is
        unknown (every app page skips a controller unless the entry's
        otaFlg is "0"). It is read again soon after the installation
        (_merge). The other controllers take the answer as usual, so one
        that stays in "OTA升级中" (or went offline in it) holds nobody back.

        Errors keep what was read before and are retried OTA_RETRY_INTERVAL
        later; they never touch the status polling, which handles errors of
        the session (taken by another device, app update demanded, rate
        limit) by itself when it meets them. A 403 is just a failed read
        (YorkApi.get_otas); a 401 only logs in again (in the API, before
        any error) with "always reclaim" on, as for any request.
        """
        if self.config_entry.pref_disable_polling:
            # Like DataUpdateCoordinator._schedule_refresh.
            _LOGGER.debug("York firmware check put off: polling is disabled for the entry")
            return OTA_RETRY_INTERVAL
        present = [dev for dev in self.devices.values() if dev.present]
        if not present:
            _LOGGER.debug("York firmware check put off: no wire controller (yet)")
            return OTA_RETRY_INTERVAL
        if self._cloud_on_hold() or not self.last_update_success:
            _LOGGER.debug("York firmware check put off: polling is on hold")
            return OTA_RETRY_INTERVAL
        home_ids = list(dict.fromkeys(dev.home_id for dev in present))
        try:
            homes = await self.api.get_otas(home_ids)
        except (YorkAuthError, YorkClientOutdatedError, YorkRateLimitError) as err:
            _LOGGER.debug("York firmware check not done (%s); left to the status polling", err)
            return OTA_RETRY_INTERVAL
        except YorkError as err:
            reason = str(err) or type(err).__name__
            if self._firmware_read_error is None:
                _LOGGER.warning(
                    "Could not read the York firmware information (%s); keeping the last one "
                    "and trying again in %s minutes",
                    reason,
                    OTA_RETRY_INTERVAL // 60,
                )
            else:
                _LOGGER.debug("York firmware information still unreadable: %s", reason)
            self._firmware_read_error = reason
            return OTA_RETRY_INTERVAL
        if self._firmware_read_error is not None:
            _LOGGER.info("York firmware information can be read again")
            self._firmware_read_error = None
        for home_id in home_ids:
            entries = home_controllers(homes, home_id, only_home=len(home_ids) == 1)
            if entries is None:
                # Malformed or not answered for this home: keep what was
                # read before.
                _LOGGER.debug("York firmware answer has nothing usable for a home; kept the last")
                continue
            for dev in present:
                if dev.home_id != home_id:
                    continue
                if dev.ota:
                    _LOGGER.debug("York %s is installing an update: entry kept", dev.code)
                    continue
                dev.firmware = entries.get(dev.code, {})
        if self._firmware_rediscover:
            # After an installed update: its versions (and maybe constants)
            # changed; the next poll reads the controller records again.
            self._firmware_rediscover = False
            self._last_discovery = None
        self.async_update_listeners()
        return OTA_INTERVAL

    # ------------------------------------------------------- session / issues

    def _yield_minutes(self) -> int:
        return int(
            self.config_entry.options.get(CONF_SESSION_YIELD_MINUTES, DEFAULT_SESSION_YIELD_MINUTES)
        )

    def _start_yield(self, now: float) -> None:
        minutes = self._yield_minutes()
        self._yield_started = now
        self._yielded_until = now + minutes * 60
        _LOGGER.warning(
            "The York account was logged in on another device; leaving the session "
            "to it for %s minutes (a command from Home Assistant takes it back now)",
            minutes,
        )
        self._create_session_issue(minutes)

    def _create_session_issue(self, minutes: int) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.config_entry, ISSUE_SESSION_TAKEN),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SESSION_TAKEN,
            translation_placeholders={"minutes": str(minutes)},
            data={"entry_id": self.config_entry.entry_id},
        )

    def _end_yield(self) -> None:
        if self._yielded_until is None:
            return
        _LOGGER.info("Logged in to the York cloud again")
        self._yielded_until = None
        self._yield_started = None
        self._relogin_backoff = 0
        # The other device may have changed anything meanwhile.
        self._full_status = True
        self._last_scene_fetch = None
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.config_entry, ISSUE_SESSION_TAKEN))

    @callback
    def async_apply_session_options(self) -> None:
        """Apply changed session options to a running yield.

        Switching to "always reclaim" ends it (the next poll logs in again);
        a different yield period moves its end.
        """
        if self._yielded_until is None or self._yield_started is None or self._relogin_backoff:
            return
        if self.api.reclaim_session:
            self._yielded_until = time.monotonic()
            return
        minutes = self._yield_minutes()
        deadline = self._yield_started + minutes * 60
        if deadline != self._yielded_until:
            self._yielded_until = deadline
            self._create_session_issue(minutes)

    def _start_outdated(self, now: float, err: YorkClientOutdatedError) -> None:
        self._outdated_until = now + CLIENT_OUTDATED_PAUSE
        _LOGGER.warning(
            "The York cloud requires a newer app version (%s); pausing polling for an hour",
            err.version or "unknown",
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.config_entry, ISSUE_CLIENT_OUTDATED),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CLIENT_OUTDATED,
            translation_placeholders={
                "version": err.version or "?",
                "url": err.url or "-",
            },
        )

    async def async_reclaim_session(self) -> None:
        """Log in again now, taking the session back from another device.

        Only on explicit user action (a command, the repair flow).
        """
        try:
            await self.api.relogin()
        except YorkAuthError:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from None
        except YorkClientOutdatedError as err:
            self._start_outdated(time.monotonic(), err)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="client_outdated"
            ) from None
        except YorkError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from None
        self._end_yield()

    # --------------------------------------------------------------- control

    async def async_request_change(self, code: str, **target: Any) -> None:
        """Send a change of the control page right away (see build_command).

        Only what differs from the current state is sent, as the app does;
        commands to one controller are sent one after another. A change the
        app does not allow in the current state raises ServiceValidationError.
        """
        dev = self.devices[code]
        profile = dev.profile
        if self._yielded_until is not None and dev.present and dev.online:
            # Taking the session back logs the phone app out: not for a
            # change this controller can never accept. Refusals that depend
            # on the live state (on/off, mode) are only decided after the
            # re-read, as the cached state can be minutes old.
            try:
                build_command(dev.view, target, profile)
            except CommandRefused as err:
                if err.key in _CAPABILITY_REFUSALS:
                    raise self._refusal_error(err) from None
        await self._run_command(
            code,
            lambda view: build_command(view, target, profile),
            optimistic=lambda view, cmd: command_state(view, cmd, profile),
        )

    async def async_send_raw(
        self,
        code: str,
        build: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        optimistic: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        fresh: bool = False,
        recheck: bool = False,
    ) -> None:
        """Send a ctrlJson that does not go through build_command.

        For the settings page and the filter reset, which the app sends
        as they are (no diff, no cancelled functions). ``build`` gets the
        device view once the per-controller lock is held (and, after taking
        the session back, the re-read state); an empty result sends nothing.
        ``fresh`` first re-reads the controller's full state (cursor 0), as
        the settings page loads its model with getXkqControlData timestamp 0
        before save() sends all of it back: a setting changed on the wall
        controller since the last poll is then not reverted. The accepted
        command is shown right away as ``optimistic(view, ctrl_json)`` (default: the
        ctrlJson itself), held against stale reports like any other command.
        ``recheck`` makes the first poll after that grace period read the
        full state (cursor 0), so the held values are checked against the
        controller even if its incremental reports leave them out.
        """
        await self._run_command(code, build, optimistic=optimistic, fresh=fresh, recheck=recheck)

    async def _run_command(
        self,
        code: str,
        build: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        optimistic: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        fresh: bool = False,
        recheck: bool = False,
    ) -> None:
        async with self._prepare_command(code, fresh=fresh) as dev:
            try:
                ctrl_json = build(dev.view)
            except CommandRefused as err:
                # Checked against the state the command is built from (just
                # re-read after taking the session back, or when fresh).
                raise self._refusal_error(err) from None
            if not ctrl_json:
                _LOGGER.debug("York %s already in the requested state", dev.code)
                return
            # A running timer was set against the current power state (it
            # toggles it); whether the cloud keeps or drops it when the power
            # changes is up to the cloud, so read its timer fields in full
            # once the grace period is over.
            recheck = recheck or (KEY_POWER in ctrl_json and timer_active(dev.view))
            state = ctrl_json if optimistic is None else optimistic(dev.view, ctrl_json)
            # State implied beyond the sent fields (e.g. a 3D unit's other
            # direction entries) is checked against a full read later.
            recheck = recheck or any(key not in ctrl_json for key in state)
            await self._send_ctrl(dev, ctrl_json)
            # Accepted: show the new state (as the app does) until the
            # controller confirms it or the grace period ends.
            self._accepted(dev, state, recheck=recheck)

    def _refusal_error(self, err: CommandRefused) -> ServiceValidationError:
        """The user-facing error for a refused change.

        The ``function`` placeholder names the function as its entity does,
        in Home Assistant's language (the error message is translated into
        that language too): Chinese for zh*, else English.
        """
        placeholders = dict(err.placeholders)
        if (func := placeholders.get("function")) in FUNC_LABELS:
            chinese = self.hass.config.language.lower().startswith("zh")
            placeholders["function"] = FUNC_LABELS[func][0 if chinese else 1]
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders=placeholders or None,
        )

    async def async_set_simple_timer(self, code: str, hours: float | None) -> None:
        """Set the simple timer (简易定时) to ``hours``, or cancel it (None).

        Like the control page's picker, a new timer always toggles the
        current power state (off timer while running, on timer while
        stopped), so the full state is re-read first (cursor 0) and the
        action decided from it. Cancelling is only sent while a timer is
        set (the page returns early without hasTimer). Refused like any
        command while offline, updating or cooling down. The first poll
        after the grace period reads the full state, so the cloud's own
        timer fields replace the local ones even if the incremental reports
        leave them out.
        """
        if hours is not None and hours not in TIMER_HOURS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_timer",
                translation_placeholders={"option": f"{hours:g}h"},
            )
        async with self._prepare_command(code, fresh=True) as dev:
            view = dev.view
            if hours is None:
                if not timer_active(view):
                    _LOGGER.debug("York %s has no timer to cancel", dev.code)
                    return
                data = await self._send_timer(dev, None, turn_on=False)
                dev.timer_choice = None
                dev.timer_check_after = None
                state = timer_cancel_state(data)
            else:
                turn_on = new_timer_turns_on(view)
                data = await self._send_timer(dev, hours, turn_on=turn_on)
                state = timer_set_state(hours, turn_on, data)
                dev.timer_choice = (state.get(KEY_TIMER_TIME), hours)
            self._accepted(dev, state, recheck=True)

    @asynccontextmanager
    async def _prepare_command(self, code: str, *, fresh: bool) -> AsyncIterator[YorkDevice]:
        """Checks and per-controller lock around one command.

        Refuses offline, updating or cooling-down controllers; takes the
        session back (re-reading the state) if it was left to another
        device, else re-reads the full state first when ``fresh``. The body
        runs with the lock held and builds its request from ``dev.view``.
        """
        dev = self.devices[code]
        if not dev.present or not dev.online:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="offline")
        if self._yielded_until is None:
            # While the session is left to the app the cached state can be
            # minutes old (no polling): the branch below re-reads and checks
            # it, so a short cooling down that has ended since does not block.
            _check_controllable(dev)
        async with self._locks.setdefault(code, asyncio.Lock()):
            if self._yielded_until is not None:
                # An explicit command takes the session back right away. The
                # state may have changed meanwhile: re-read it before diffing
                # (the scene lists wait for the next regular poll).
                await self.async_reclaim_session()
                try:
                    await self._poll(scenes=False)
                except (UpdateFailed, ConfigEntryAuthFailed) as err:
                    # Not a tolerated failure here: never diff a command
                    # against state that could not be re-read.
                    if isinstance(err, ConfigEntryAuthFailed):
                        self.config_entry.async_start_reauth(self.hass)
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="auth_failed"
                        ) from None
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="cannot_connect",
                        translation_placeholders={"error": str(err)},
                    ) from None
                self._failures = 0
                self.async_set_updated_data(self.devices)
                _check_controllable(dev)
            elif fresh:
                # (After taking the session back the state was just re-read.)
                await self._read_full_status(dev)
            yield dev

    @callback
    def _accepted(self, dev: YorkDevice, state: dict[str, Any], *, recheck: bool) -> None:
        """Show an accepted command's ``state`` and poll again soon."""
        _apply_optimistic(dev, state)
        if recheck:
            dev.recheck_after = time.monotonic() + COMMAND_GRACE
        self.async_update_listeners()
        self._schedule_control_refresh()

    async def _read_full_status(self, dev: YorkDevice) -> None:
        """Re-read one controller's full state (cursor 0) before a command.

        Cloud errors map to the same user-facing errors as sending; a
        controller that turns out to be updating its firmware or cooling
        down is refused.
        """
        try:
            statuses = await self.api.get_status(dev.home_id, [(dev.code, 0)])
        except YorkError as err:
            raise self._command_error(err) from None
        if (status := statuses.get(dev.code)) is None:
            # Never build a command from state that could not be re-read.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": "no status in the reply"},
            )
        self._merge(dev, status)
        self.async_update_listeners()
        _check_controllable(dev)

    async def _send_ctrl(self, dev: YorkDevice, ctrl_json: dict[str, Any]) -> None:
        """Send ctrlJson, mapping cloud errors to user-facing ones."""
        try:
            await self.api.control(dev.home_id, dev.code, dev.device_type, dev.xkq_type, ctrl_json)
        except YorkError as err:
            raise self._command_error(err) from None

    async def _send_timer(
        self, dev: YorkDevice, hours: float | None, *, turn_on: bool
    ) -> dict[str, Any]:
        """Set (``hours``) or cancel (None) the timer, mapping cloud errors."""
        try:
            if hours is None:
                return await self.api.cancel_simple_timer(dev.home_id, dev.code)
            return await self.api.set_simple_timer(dev.home_id, dev.code, hours, turn_on=turn_on)
        except YorkError as err:
            raise self._command_error(err) from None

    def _command_error(self, err: YorkError) -> HomeAssistantError:
        """The user-facing error for a cloud error during a command."""
        if isinstance(err, YorkSessionTakenError):
            # Taken again right after the forced login: the credentials are
            # fine, the other device simply took the session back. Leave it
            # to that device rather than asking to log in again.
            if not self.api.reclaim_session:
                self._start_yield(time.monotonic())
            return HomeAssistantError(translation_domain=DOMAIN, translation_key="session_taken")
        if isinstance(err, YorkAuthError):
            self.config_entry.async_start_reauth(self.hass)
            return HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed")
        if isinstance(err, YorkClientOutdatedError):
            self._start_outdated(time.monotonic(), err)
            return HomeAssistantError(translation_domain=DOMAIN, translation_key="client_outdated")
        if isinstance(err, YorkCommandError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"message": str(err)},
            )
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        )

    # ---------------------------------------------------------- scene actions

    async def async_run_scene(self, home_id: int, smart_id: str) -> None:
        """Run a scene like the app's run button.

        Decided on the home's scene list read right before (_prepare_scene):
        a scene without devices is refused, an edited one runs its current
        commands. Returns once the cloud accepted the request (code "200");
        for a replayed scene the cloud's result is followed in the background
        like the scenes tab does (multiCtrlResult), and a failure is logged.
        """
        async with self._prepare_scene(home_id, smart_id, RUN) as scene:
            _check_scene_devices(scene)
            stype = scene.stype
            assert stype is not None  # (checked by _scene)
            record = scene.record
            try:
                if stype.request == REPLAY:
                    request = replay_request(record, stype, self.homes[home_id].flags)
                    tokens = await self.api.run_scene(
                        scene.request_home_id,
                        request["ctrl_type"],
                        request["xkq_ctrl_list"],
                        request["ctrl_list"],
                        scene_id=request["scene_id"],
                    )
                else:
                    data = await self.api.run_recommended_scene(
                        scene.request_home_id,
                        record.get("type"),
                        record.get("smartId"),
                        record.get("updateTime"),
                    )
            except YorkError as err:
                raise self._scene_error(err, scene) from None
            finally:
                self._after_scene_action(home_id)
            if stype.request == REPLAY:
                _LOGGER.debug("York scene %s accepted by the cloud", scene.smart_id)
                self._release_held(record)
                if tokens:
                    self.config_entry.async_create_background_task(
                        self.hass,
                        self._scene_result(scene.name, tokens),
                        f"{DOMAIN} scene result",
                    )
            else:
                _store_update_time(scene, data)

    async def async_set_scene(self, home_id: int, smart_id: str, on: bool) -> None:
        """Switch a scene on or off like the app's toggle.

        Decided on the home's scene list read right before (_prepare_scene):
        nothing is sent if the scene is already so; like the app, a scene
        without devices (or a linkage / pets scene without its air box)
        cannot be switched on, while switching off is always sent (the
        home page allows it for most types). The explicit target ("1"/"0")
        goes with the scene's current updateTime. A conflict with another
        scene is an error: HA never forces it (forceOpenFlg / flag "1").
        """
        async with self._prepare_scene(home_id, smart_id, SWITCH) as scene:
            stype = scene.stype
            assert stype is not None  # (checked by _scene)
            record = scene.record
            if scene_target_reached(record, stype, on):
                _LOGGER.debug("York scene %s is already %s", scene.smart_id, "on" if on else "off")
                return
            if on:
                _check_scene_devices(scene)
            flag = OPEN if on else CLOSE
            try:
                if stype.request == OPEN_FLAG:
                    data = await self.api.set_scene_open(
                        scene.request_home_id, record.get("smartId"), flag, record.get("updateTime")
                    )
                elif stype.request == RECOMMENDED:
                    data = await self.api.run_recommended_scene(
                        scene.request_home_id,
                        record.get("type"),
                        record.get("smartId"),
                        record.get("updateTime"),
                        open_flg=flag,
                    )
                else:
                    data = await self.api.set_auto_scene(
                        scene.request_home_id,
                        record.get("smartId"),
                        record.get("type"),
                        flag,
                        record.get("updateTime"),
                    )
            except YorkError as err:
                raise self._scene_error(err, scene) from None
            finally:
                self._after_scene_action(home_id)
            # Accepted: show the new state as the app does until the list is
            # read again; the answer's updateTime is the next change's token.
            # Laid over the record as it is now: a status poll may have read
            # the list while the request was out (one sent after this
            # action's own read; an older one is dropped, _fetch_scenes).
            scene.record = {**scene.record, flag_key(stype): flag}
            _store_update_time(scene, data)
            self.async_update_listeners()

    @asynccontextmanager
    async def _prepare_scene(
        self, home_id: int, smart_id: str, control: str
    ) -> AsyncIterator[YorkScene]:
        """Per-home lock, session and a fresh scene list around one scene action.

        The scene must be one HA shows (_scene, on the list as last read);
        for anything else nothing is sent, and a session left to another
        device is not taken back for it. With the lock held, a session left
        to another device is taken back (an action is explicit intent) and
        the home's scene list is read again before the body decides: the
        scene's devices, on/off state, stored commands and updateTime token
        can all have changed in the app since the last read, which can be
        SCENE_INTERVAL old, or as old as the yield. The app decides on a list
        it has just read too (the scenes tab reads it each time it opens).
        """
        self._scene(home_id, smart_id, control)
        async with self._scene_locks.setdefault(home_id, asyncio.Lock()):
            if self._yielded_until is not None:
                await self.async_reclaim_session()
            try:
                result = await self.api.get_scenes(home_id, reclaim=True)
            except YorkError as err:
                raise self._command_error(err) from None
            _merge_scenes(self.homes[home_id], result)
            # A poll's read of this list that is still out is older.
            self._count_scene_action(home_id)
            self.async_update_listeners()
            yield self._scene(home_id, smart_id, control)

    @callback
    def _count_scene_action(self, home_id: int) -> None:
        """A scene action of ``home_id`` took its fresh list or got its answer.

        A list read of that home in a status poll that is out meanwhile may
        predate it; its answer is dropped (_fetch_scenes).
        """
        self._scene_actions += 1
        self._scene_action_at[home_id] = self._scene_actions

    @callback
    def _after_scene_action(self, home_id: int) -> None:
        """After any scene request: read the scene lists again and poll soon.

        A status poll that is reading the lists meanwhile drops the answer
        for ``home_id`` and does not count as that read (_poll).
        """
        self._count_scene_action(home_id)
        self._last_scene_fetch = None
        self._schedule_control_refresh()

    @callback
    def _release_held(self, record: dict[str, Any]) -> None:
        """A run's commands replace values held from earlier commands.

        Values just set from HA are held against stale reports for
        COMMAND_GRACE; once a scene ran, the controllers' own reports win.
        """
        xkq_list = record.get("xkqList")
        for entry in xkq_list if isinstance(xkq_list, list) else []:
            if isinstance(entry, dict) and (dev := self.devices.get(str(entry.get("xkqCode")))):
                dev.expected.clear()

    async def _scene_result(self, name: str, tokens: list[Any]) -> None:
        """Follow a scene run like the scenes tab's getControlResults.

        multiCtrlResult every SCENE_RESULT_INTERVAL: "200" ends it as done,
        one of the failure codes as failed (with the cloud's message), any
        other code keeps polling, and so does a request that fails (the app
        only logs it and asks again at the next tick). After
        SCENE_RESULT_TIMEOUT the run counts as done if any module reported
        "2". The app shows a toast; HA logs a failure as a warning. The
        check stops without a verdict, sending nothing more, while the
        session is left to another device or the cloud asked to pause, and
        on an error of the session, an app update demand or a rate limit
        (with "always reclaim" on, the API first logs in again on a 401, as
        for any request).
        """
        data: Any = None
        for _ in range(SCENE_RESULT_POLLS):
            await asyncio.sleep(SCENE_RESULT_INTERVAL)
            if self._cloud_on_hold():
                _LOGGER.debug("York scene %s: result not checked, polling is on hold", name)
                return
            try:
                answer = await self.api.get_control_results(tokens)
            except (YorkAuthError, YorkClientOutdatedError, YorkRateLimitError) as err:
                _LOGGER.debug("York scene %s: result not checked further (%s)", name, err)
                return
            except YorkError as err:
                _LOGGER.debug("York scene %s: result not readable (%s), asking again", name, err)
                continue
            if isinstance(answer.get("data"), list):
                data = answer["data"]
            outcome = result_outcome(answer["code"])
            if outcome is True:
                _LOGGER.debug("York scene %s carried out", name)
                return
            if outcome is False:
                _LOGGER.warning(
                    "York scene %s failed: %s",
                    name,
                    answer.get("message") or f"code {answer['code']}",
                )
                return
        if module_succeeded(data):
            _LOGGER.debug("York scene %s carried out by at least one device", name)
        else:
            _LOGGER.warning(
                "York scene %s: no device confirmed it within %s seconds",
                name,
                SCENE_RESULT_TIMEOUT,
            )

    def _cloud_on_hold(self) -> bool:
        """The session is left to another device, or the cloud asked to pause."""
        now = time.monotonic()
        return (
            self._yielded_until is not None
            or self._outdated_until is not None
            or (self._paused_until is not None and now < self._paused_until)
        )

    def _scene_error(self, err: YorkError, scene: YorkScene) -> HomeAssistantError:
        """The user-facing error for a failed scene request."""
        if isinstance(err, YorkSceneConflictError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="scene_conflict",
                translation_placeholders={"scene": scene.name, "message": str(err)},
            )
        if isinstance(err, YorkCommandError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="scene_rejected",
                translation_placeholders={"scene": scene.name, "message": str(err)},
            )
        return self._command_error(err)

    @callback
    def _schedule_control_refresh(self) -> None:
        # Not "_schedule_refresh": that name belongs to DataUpdateCoordinator,
        # which uses it to schedule the regular polling.
        self._cancel_control_refresh()
        self._unsub_control_refresh = async_call_later(
            self.hass, REFRESH_DELAY_AFTER_CONTROL, self._control_refresh
        )

    async def _control_refresh(self, _now: datetime) -> None:
        self._unsub_control_refresh = None
        await self.async_request_refresh()

    @callback
    def _cancel_control_refresh(self) -> None:
        if self._unsub_control_refresh is not None:
            self._unsub_control_refresh()
            self._unsub_control_refresh = None


def _check_controllable(dev: YorkDevice) -> None:
    """Refuse commands the app refuses for the controller's current state."""
    if dev.ota:
        # Home page: "线控器正在升级中，无法控制线控器。"
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="ota_in_progress")
    if dev.cooling_down:
        # Control page: self_clean == 2 opens the self-clean dialog in its
        # "系统冷却中" state, which only leaves the page; the home page covers
        # the card with the same overlay.
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cooling_down")


def _check_scene_devices(scene: YorkScene) -> None:
    """Refuse running or switching on a scene the app refuses.

    Without devices; for a linkage or pets scene, also without its air box.
    """
    if not has_devices(scene.record):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="scene_no_devices",
            translation_placeholders={"scene": scene.name},
        )
    if (stype := scene.stype) is not None and not has_sensor(scene.record, stype):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="scene_no_sensor",
            translation_placeholders={"scene": scene.name},
        )


def _merge_scenes(home: YorkHome, result: HomeScenes) -> None:
    """Take a home's scene list and flags; scenes missing from it are marked gone."""
    home.flags = dict(result.flags)
    seen = set()
    for record in result.scenes:
        smart_id = str(record["smartId"])
        seen.add(smart_id)
        if (scene := home.scenes.get(smart_id)) is None:
            _LOGGER.debug("York scene %s (type %s) found", smart_id, record.get("type"))
            home.scenes[smart_id] = YorkScene(home.home_id, record)
        else:
            scene.record = record
    for smart_id, scene in home.scenes.items():
        scene.present = smart_id in seen
        scene.offered = scene_offered(scene.record, home.flags)
    home.loaded = True


def _store_update_time(scene: YorkScene, data: dict[str, Any]) -> None:
    """Keep the updateTime of an accepted change: the token of the next one.

    The correctly spelt field only (the scenes tab reads a misspelt
    "upadteTime" after running a greenhouse or party scene). Without it the
    scene list, which is read again after every action, brings the token.
    """
    if (token := data.get("updateTime")) is not None:
        scene.record = {**scene.record, "updateTime": token}


def _has_value(value: Any) -> bool:
    """Whether a constant overrides the status.

    The app copies a constant only if it is truthy in JavaScript, so a null
    or empty constant leaves the live value alone (the string "0" is truthy
    and kept). Numeric 0 is kept too: HA reads e.g.
    is_set_temperature05_avalable 0 as "whole degrees only".
    """
    return value is not None and value != ""


def _clear_exclusive(status: dict[str, Any], update: dict[str, Any]) -> None:
    """A newly selected mode/fan flag clears the other flags of its group."""
    for group in _EXCLUSIVE_GROUPS:
        selected = [k for k in group if k in update and is_on(update[k])]
        if selected:
            for key in group:
                if key not in selected:
                    status[key] = "0"


def _apply_optimistic(dev: YorkDevice, update: dict[str, Any]) -> None:
    """Show an accepted command's fields and hold them for COMMAND_GRACE."""
    before = dict(dev.status)
    dev.status.update(update)
    _clear_exclusive(dev.status, update)
    deadline = time.monotonic() + COMMAND_GRACE
    for key, value in dev.status.items():
        if key in update or before.get(key) != value:
            dev.expected[key] = (value, deadline)
    _schedule_timer_check(dev)


def _merge_status(dev: YorkDevice, update: dict[str, Any]) -> None:
    """Merge a status report, ignoring reports older than what we have.

    The status is merged rather than replaced so a partial report cannot
    wipe fields. Reports are ordered by the same field used as the polling
    cursor (roundTime, else timestamp).
    """
    new_cursor = report_cursor(update)
    if dev.cursor and new_cursor and new_cursor < dev.cursor:
        _LOGGER.debug("Ignoring stale status for %s", dev.code)
        return
    if new_cursor:
        dev.cursor = new_cursor
    update = _hold_expected(dev, update)
    dev.status.update(update)
    _clear_exclusive(dev.status, update)
    if dev.timer_choice is not None:
        pinned, hours = dev.timer_choice
        end = as_number(dev.status.get(KEY_TIMER_TIME))
        if not timer_active(dev.status):
            # The timer set from HA has fired or was cancelled elsewhere.
            dev.timer_choice = None
        elif end is not None and KEY_TIMER_TIME not in dev.expected:
            if pinned is None:
                # The answer had no time: the first end the cloud reports after
                # the grace period is this timer's (a later different end is a
                # timer replaced elsewhere).
                dev.timer_choice = (int(end), hours)
            elif int(end) != pinned and abs(end - pinned) <= TIMER_PIN_TOLERANCE_MS:
                # Seen live: the answer's end is computed by the cloud when it
                # takes the request, the reported one by the controller when it
                # runs it, a few seconds later. Same timer: follow the report.
                dev.timer_choice = (int(end), hours)
    _schedule_timer_check(dev)


def _schedule_timer_check(dev: YorkDevice) -> None:
    """Read the full state once, TIMER_CHECK_DELAY after the timer's end.

    Covers a timer that fires on its own (no command from HA): without it,
    the fired timer would show until the next full read (discovery).
    """
    now_ms = time.time() * 1000
    end = timer_end_ms(dev.status, now_ms)
    if end is None or end == dev.timer_check_end:
        return
    dev.timer_check_end = end
    dev.timer_check_after = time.monotonic() + max(0.0, (end - now_ms) / 1000) + TIMER_CHECK_DELAY


def _hold_expected(dev: YorkDevice, update: dict[str, Any]) -> dict[str, Any]:
    """Keep just-commanded values against reports sent before they applied."""
    if not dev.expected:
        return update
    now = time.monotonic()
    update = dict(update)
    for key, (value, deadline) in list(dev.expected.items()):
        if now >= deadline:
            del dev.expected[key]
        elif key in update:
            if str(update[key]) == str(value):
                del dev.expected[key]  # confirmed by the controller
            else:
                update[key] = value
    return update
