"""Async client for the York Smart Home (约克智慧家) cloud API.

The API is the Hisense-Hitachi "hicloud" backend used by the 约克智慧家 app
(com.hisensehitachi.iyes2). York units are reached through a Wi-Fi wire
controller ("线控器", called "xkq" in the API).

This module has no Home Assistant dependency so it can be tested standalone.
Error messages never include URLs or response bodies: the login request
carries the password as a query parameter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import math
import secrets
import time
from typing import Any

import aiohttp

from .const import (
    API_BASE,
    API_KEY,
    APP_ID,
    APP_TAG,
    APP_VERSION,
    CTRL_TYPE_SINGLE,
    TIMER_HOURS,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)
# If a renewed session is rejected again this soon, another login replaced it.
_SESSION_CONFLICT_WINDOW = 600

# Wire controllers speaking the protocol implemented here (York "公建云" type C).
# The home page routes xkqType "9" to the colour controller page
# (xkq-color-control) and "8" to the standard one (xkq-control); both use
# the same status, control and timer endpoints (see profile.py).
SUPPORTED_BRAND = "3"
SUPPORTED_TYPE = "C"
XKQ_TYPE_COLOR = "9"
XKQ_TYPE_STANDARD = "8"
SUPPORTED_XKQ_TYPES = (XKQ_TYPE_COLOR, XKQ_TYPE_STANDARD)


class YorkError(Exception):
    """Base error."""


class YorkConnectionError(YorkError):
    """The cloud could not be reached or failed temporarily."""


class YorkRateLimitError(YorkConnectionError):
    """HTTP 429; the server asked us to slow down."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class YorkApiError(YorkError):
    """Unsuccessful or malformed API response."""


class YorkAuthError(YorkError):
    """The session was rejected and could not be renewed."""


class YorkLoginError(YorkAuthError):
    """A login endpoint refused the credentials; ``message`` is the server's text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class YorkSessionConflictError(YorkAuthError):
    """A fresh session was revoked right away: the account logged in elsewhere."""


class YorkSessionTakenError(YorkAuthError):
    """HTTP 401 / code 401: the account logged in on another device.

    The app detects this (LOGIN_ELSEWHERE, "该账号已在其他手机登录") from the
    HTTP 401 status only and never logs in again by itself; a body code
    "401" is treated the same way as a precaution. Raised instead of renewing the session unless
    the client was asked to reclaim it.
    """


class YorkClientOutdatedError(YorkError):
    """Business code "300": the cloud requires a newer app (APP_FORCE_UPDATE)."""

    def __init__(self, version: str | None, url: str | None) -> None:
        super().__init__(f"app update required (version {version or '?'})")
        self.version = version
        self.url = url


class YorkCommandError(YorkError):
    """The cloud or the wire controller rejected a command."""


def is_on(value: Any) -> bool:
    """The API mixes "1", 1 and True for flags."""
    return value is True or value == 1 or value == "1"


def as_number(value: Any) -> float | None:
    """Numeric value, or None for missing, boolean or non-numeric input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_timestamp(value: Any) -> int:
    number = as_number(value)
    return int(number) if number and number > 0 else 0


def parse_constants(value: Any) -> dict[str, Any]:
    """constantJson as a dict; the API may also send it as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def new_client_id() -> str:
    """Push registration id sent on login.

    The server rejects an empty jgRegId (code 400). Use our own id rather than
    the phone's, so pushes meant for the app are not redirected to HA.
    """
    return secrets.token_hex(10)[:19]


def is_supported(record: dict[str, Any]) -> bool:
    return (
        str(record.get("type")) == SUPPORTED_TYPE
        and str(record.get("xkqType")) in SUPPORTED_XKQ_TYPES
        and str(record.get("brand", SUPPORTED_BRAND)) == SUPPORTED_BRAND
    )


@dataclass
class Session:
    """Everything needed to restore or renew a login."""

    token: str
    user_id: int
    refresh_token: str | None = None
    login_version: int = 0


class YorkApi:
    """Client covering login, discovery, status and control for one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        phone: str,
        *,
        password: str | None = None,
        auth: Session | None = None,
        client_id: str | None = None,
        on_session: Callable[[Session], None] | None = None,
        reclaim_session: bool = False,
    ) -> None:
        self._http = session
        self._phone = phone
        self._password = password
        self.auth = auth
        self.client_id = client_id or new_client_id()
        self._on_session = on_session
        self._login_lock = asyncio.Lock()
        self._last_renewal: float | None = None
        # True: log in again as soon as another device takes the session
        # (guarded against ping-pong); False: raise YorkSessionTakenError.
        self.reclaim_session = reclaim_session
        self.unsupported_count = 0

    @property
    def user_id(self) -> int | None:
        return self.auth.user_id if self.auth else None

    # ------------------------------------------------------------------ http

    def _headers(self) -> dict[str, str]:
        token = self.auth.token if self.auth else ""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-His-AppId": APP_ID,
            "X-His-AppTag": APP_TAG,
            "X-His-APIKey": API_KEY,
            "X-His-Version": APP_VERSION,
            "X-His-OS": "Android",
            "X-His-Locale": "zh_CN",
            "X-His-Timestamp": str(int(time.time() * 1000)),
            "Authorization": f"Bearer {token}" if token else "Bearer",
        }

    async def _envelope(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        """One request; returns the ``{"code", "message", "data"}`` envelope."""
        try:
            async with self._http.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json,
                headers=self._headers(),
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                if resp.status == 401:
                    raise YorkSessionTakenError(f"{path}: HTTP 401")
                if resp.status == 403:
                    raise YorkAuthError(f"{path}: HTTP 403")
                if resp.status == 429:
                    retry = resp.headers.get("Retry-After", "")
                    raise YorkRateLimitError(
                        min(max(int(retry), 30), 3600) if retry.isdigit() else 60
                    )
                if resp.status >= 500:
                    raise YorkConnectionError(f"{path}: HTTP {resp.status}")
                if resp.status != 200:
                    raise YorkApiError(f"{path}: HTTP {resp.status}")
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    raise YorkApiError(f"{path}: invalid JSON") from None
        except (aiohttp.ClientError, TimeoutError) as err:
            # Do not chain: aiohttp errors carry the URL (with the password on /login).
            raise YorkConnectionError(f"{path}: {type(err).__name__}") from None

        if not isinstance(body, dict) or "code" not in body:
            raise YorkApiError(f"{path}: invalid response envelope")
        body["code"] = str(body["code"])
        if body["code"] == "401":
            # The app only checks the HTTP status for this; treated alike to be safe.
            raise YorkSessionTakenError(f"{path}: code 401")
        if body["code"] == "403":
            raise YorkAuthError(f"{path}: code 403")
        if body["code"] == "300":
            # handleResponse's only special code: force an app update.
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            raise YorkClientOutdatedError(
                str(data["version"]) if data.get("version") else None,
                str(data["url"]) if data.get("url") else None,
            )
        return body

    async def _raw(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request; returns ``data`` of a successful envelope."""
        body = await self._envelope(method, path, **kwargs)
        if body["code"] != "200":
            raise YorkApiError(f"{path}: code {body['code']} {body.get('message') or ''}".rstrip())
        return body.get("data")

    async def _authed(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        envelope: bool = False,
        reclaim: bool = False,
    ) -> Any:
        """Authenticated request; renews the session once if it was rejected.

        A session taken by another device (401) is only renewed when
        ``reclaim`` (an explicit user action) or ``reclaim_session`` is set;
        otherwise YorkSessionTakenError is raised so the caller can leave the
        session to that device, as the app does.
        """
        if self.auth is None:
            await self._renew(None)
        call = self._envelope if envelope else self._raw
        for attempt in (1, 2):
            used = self.auth
            try:
                return await call(method, path, json=json)
            except YorkSessionTakenError:
                if attempt == 2 or not (reclaim or self.reclaim_session):
                    raise
                _LOGGER.debug("Session taken by another device on %s, logging in again", path)
                # An explicit action takes the session back even right after
                # a renewal; automatic reclaiming stops on ping-pong.
                await self._renew(used, guard=not reclaim)
            except YorkAuthError:
                if attempt == 2:
                    raise
                _LOGGER.debug("Session rejected on %s, renewing", path)
                await self._renew(used)
        raise AssertionError("unreachable")

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        return await self._authed(method, path, json=json)

    # ------------------------------------------------------------------ auth

    def _accept_login(self, data: Any) -> Session:
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not user.get("token") or user.get("userId") is None:
            raise YorkApiError("login: response has no session")
        user_id = int(user["userId"])
        if self.auth is not None and self.auth.user_id != user_id:
            raise YorkAuthError("login: session belongs to a different account")
        # The app only stores a refreshToken on SMS logins; a password login
        # without one must not drop the token saved earlier.
        refresh_token = user.get("refreshToken") or (
            self.auth.refresh_token if self.auth is not None else None
        )
        self.auth = Session(
            token=user["token"],
            user_id=user_id,
            refresh_token=refresh_token,
            login_version=int(user.get("version") or 0),
        )
        _LOGGER.debug("Logged in as user %s", user_id)
        if self._on_session:
            self._on_session(self.auth)
        return self.auth

    async def _login_request(self, path: str, params: dict[str, Any]) -> Session:
        try:
            body = await self._envelope("GET", path, params={**params, "jgRegId": self.client_id})
        except YorkSessionTakenError as err:
            # A login endpoint refusing us is a credentials problem.
            raise YorkAuthError(str(err)) from None
        if body["code"] != "200":
            # The app just shows the server's message; codes are not documented.
            raise YorkLoginError(str(body.get("message") or f"code {body['code']}"))
        return self._accept_login(body.get("data"))

    async def login_password(self) -> Session:
        if not self._password:
            raise YorkAuthError("no password configured")
        # The app sends the version of its previous login (lastLoginNum).
        version = self.auth.login_version if self.auth else 0
        return await self._login_request(
            "/login", {"phoneNo": self._phone, "password": self._password, "version": version}
        )

    async def request_sms_code(self) -> None:
        """Ask the cloud to text a verification code to the phone."""
        body = await self._envelope(
            "POST", "/smsLogin/getVerifyCode", json={"phoneNo": self._phone}
        )
        if body["code"] != "200":
            raise YorkLoginError(str(body.get("message") or f"code {body['code']}"))

    async def login_sms(self, code: str) -> Session:
        return await self._login_request(
            "/smsLogin",
            {"phoneNo": self._phone, "version": 0, "verifyCode": code, "refreshToken": ""},
        )

    async def login_refresh(self) -> Session:
        """Renew with the refresh token, as the app's automatic login does."""
        if self.auth is None or not self.auth.refresh_token:
            raise YorkAuthError("no refresh token")
        return await self._login_request(
            "/smsLogin",
            {
                "phoneNo": self._phone,
                "version": self.auth.login_version,
                "refreshToken": self.auth.refresh_token,
            },
        )

    async def relogin(self) -> Session:
        """Log in again now (password, or refresh token for SMS sessions).

        Used to take the session back from another device on purpose, so
        the ping-pong guard does not apply.
        """
        before = self.auth
        async with self._login_lock:
            # A concurrent caller (poll or command) may just have logged in.
            if self.auth is None or self.auth is before:
                await self._login()
                self._last_renewal = time.monotonic()
            assert self.auth is not None
            return self.auth

    async def _login(self) -> None:
        if self._password:
            await self.login_password()
        else:
            await self.login_refresh()

    async def _renew(self, rejected: Session | None, *, guard: bool = True) -> None:
        async with self._login_lock:
            # Another request may already have renewed the session.
            if self.auth is not None and self.auth is not rejected:
                return
            now = time.monotonic()
            if (
                guard
                and rejected is not None
                and self._last_renewal is not None
                and now - self._last_renewal < _SESSION_CONFLICT_WINDOW
            ):
                # Logging in again would just kick the other client out, which
                # would then kick us out: stop and let the user decide.
                raise YorkSessionConflictError(
                    "session revoked again shortly after renewal; "
                    "the account is probably logged in elsewhere"
                )
            await self._login()
            if rejected is not None:
                self._last_renewal = now

    # ------------------------------------------------------------- discovery

    async def get_wire_controllers(self) -> list[dict[str, Any]]:
        """Supported wire controllers (xkq) across all homes of the account.

        Items are raw xkq records (``xkqCode``, ``xkqName``, ``type``,
        ``xkqType``, ``constantJson`` ...) with ``homeId``/``homeName`` added.
        Other controller protocols are skipped and counted in
        ``unsupported_count``.
        """
        data = await self._request("GET", "/api/apphome/homes/homeList/deviceList")
        homes = (data or {}).get("allHomeDeviceList")
        if not isinstance(homes, list):
            raise YorkApiError("deviceList: invalid response")
        devices = []
        unsupported = 0
        for home in homes:
            if not isinstance(home, dict) or home.get("homeId") is None:
                continue
            for xkq in home.get("xkqList") or []:
                if not isinstance(xkq, dict) or not xkq.get("xkqCode"):
                    continue
                if not is_supported(xkq):
                    unsupported += 1
                    continue
                devices.append({**xkq, "homeId": home["homeId"], "homeName": home.get("homeName")})
            # Gateway (iEZ) units and air boxes use other protocols.
            unsupported += len(home.get("deviceList") or []) + len(home.get("boxList") or [])
        self.unsupported_count = unsupported
        return devices

    async def get_wire_controller_detail(self, home_id: int, xkq_code: str) -> dict[str, Any]:
        """Constants of one controller (XKQ_FETCH_DETAIL).

        The app's control page reads capabilities, locks and temperature
        limits only from this ``constantJson``, fetched each time the page
        opens. Returns the constants (empty if the answer has none).
        """
        data = await self._request("GET", f"/api/appdevice/homes/{home_id}/xkqs/{xkq_code}")
        if not isinstance(data, dict):
            raise YorkApiError("xkq detail: invalid response")
        return parse_constants(data.get("constantJson"))

    # ---------------------------------------------------------------- status

    async def get_status(
        self, home_id: int, controllers: list[tuple[str, int]]
    ) -> dict[str, dict[str, Any]]:
        """Status reports newer than the given timestamps, keyed by xkqCode.

        ``controllers`` holds (xkqCode, timestamp of the last report seen).
        Like the app's home page, timestamp 0 asks for the current state and
        a later timestamp only returns a report if the controller sent a
        newer one; controllers without news are absent from the result.
        """
        data = await self._request(
            "POST",
            f"/api/appstatus/homes/{home_id}/status",
            json={
                "homeId": home_id,
                "boxList": [],
                "iuIdList": [],
                "iuIdListGjy": [],
                "xkqListGjy": [{"xkqCode": c, "timestamp": ts} for c, ts in controllers],
            },
        )
        return {
            s["xkqCode"]: s
            for s in (data or {}).get("xkqStatusListGjy") or []
            if isinstance(s, dict) and s.get("xkqCode")
        }

    async def get_online(self, devices: list[tuple[str, str]]) -> dict[str, bool]:
        """Online flag per xkqCode. ``devices`` is a list of (xkqCode, deviceType)."""
        data = await self._request(
            "POST",
            "/api/appcontrol/cmds/xkqOnlineStatus",
            json={"xkqCtrlList": [{"xkqCode": c, "deviceType": t} for c, t in devices]},
        )
        return {
            d["xkqCode"]: is_on(d.get("online"))
            for d in data or []
            if isinstance(d, dict) and "xkqCode" in d
        }

    # --------------------------------------------------------------- control

    async def control(
        self,
        home_id: int,
        xkq_code: str,
        device_type: str,
        xkq_type: str,
        ctrl_json: dict[str, Any],
    ) -> None:
        """Send a command the way the app's control page does.

        The page treats envelope code "200" as success and does not poll
        multiCtrlResult (only the home page's quick power switch does); the
        new state then shows up through the normal status polling.

        A command is an explicit user action, so a session taken by another
        device is logged in again right away.
        """
        _LOGGER.debug("Control %s: %s", xkq_code, ctrl_json)
        body = await self._authed(
            "POST",
            "/api/appcontrol/cmds/multiIuOuCtrl",
            json={
                "homeId": home_id,
                "ctrlType": CTRL_TYPE_SINGLE,
                "ctrlList": [],
                "xkqCtrlList": [
                    {
                        "xkqCode": xkq_code,
                        "deviceType": device_type,
                        "xkqType": xkq_type,
                        "ctrlJson": ctrl_json,
                    }
                ],
            },
            envelope=True,
            reclaim=True,
        )
        _LOGGER.debug("Control %s answered: %s", xkq_code, body)
        if body["code"] != "200":
            raise YorkCommandError(str(body.get("message") or f"code {body['code']}"))

    # ----------------------------------------------------------- simple timer

    async def set_simple_timer(
        self, home_id: int, xkq_code: str, hours: float, *, turn_on: bool
    ) -> dict[str, Any]:
        """Set the one-shot timer (简易定时) as the control page's picker does.

        POST .../xkqs/{xkqCode}/simpleTimer (XKQ_SET_TIMING) with
        ``{"timerSimpleMinute": 60*h, "ctrlJson": {"timer_switch": "1"|"0",
        "simple_timer_time": 2*h}}``: the page parses "<h>小时" and sends both
        as numbers; timer_switch is the string "1" to turn the unit on at the
        end (it is off now) or "0" to turn it off. ``hours`` must be one of
        the picker's durations (TIMER_HOURS).

        Returns the answer's ``data``; the app reads ``timerSimpleTime``
        (when it fires, epoch ms) and ``systemTime`` (the server clock) from it.
        """
        if hours not in TIMER_HOURS:
            raise ValueError(f"unsupported timer duration: {hours}")
        return await self._simple_timer(
            home_id,
            xkq_code,
            {
                "timerSimpleMinute": int(hours * 60),
                "ctrlJson": {
                    "timer_switch": "1" if turn_on else "0",
                    "simple_timer_time": int(hours * 2),
                },
            },
        )

    async def cancel_simple_timer(self, home_id: int, xkq_code: str) -> dict[str, Any]:
        """Cancel the timer like the picker's "关闭定时".

        The page posts to the same URL with ``{"timerSimpleMinute": 0,
        "ctrlJson": {"simple_timer_time": 0}}``; the service's DELETE
        .../simpleTimer/{timerSimpleTime} (XKQ_DELETE_TIMING) is never used.
        """
        return await self._simple_timer(
            home_id, xkq_code, {"timerSimpleMinute": 0, "ctrlJson": {"simple_timer_time": 0}}
        )

    async def _simple_timer(
        self, home_id: int, xkq_code: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST a simpleTimer body; an explicit user action like control().

        The page does not check the answer's code (it shows success and
        reads ``data``); here anything but "200" is a rejected command, as
        for control().
        """
        _LOGGER.debug("Simple timer %s: %s", xkq_code, body)
        answer = await self._authed(
            "POST",
            f"/api/appdevice/homes/{home_id}/xkqs/{xkq_code}/simpleTimer",
            json=body,
            envelope=True,
            reclaim=True,
        )
        _LOGGER.debug("Simple timer %s answered: %s", xkq_code, answer)
        if answer["code"] != "200":
            raise YorkCommandError(str(answer.get("message") or f"code {answer['code']}"))
        data = answer.get("data")
        return dict(data) if isinstance(data, dict) else {}
