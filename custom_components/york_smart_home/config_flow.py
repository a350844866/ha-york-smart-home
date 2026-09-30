"""Config flow for York Smart Home."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import (
    YorkApi,
    YorkAuthError,
    YorkClientOutdatedError,
    YorkConnectionError,
    YorkError,
    YorkLoginError,
    new_client_id,
)
from .const import (
    CONF_CLIENT_ID,
    CONF_PHONE,
    CONF_RECLAIM_SESSION,
    CONF_SCAN_INTERVAL,
    CONF_SESSION,
    CONF_SESSION_YIELD_MINUTES,
    CONF_VERIFY_CODE,
    DEFAULT_RECLAIM_SESSION,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SESSION_YIELD_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MAX_SESSION_YIELD_MINUTES,
    MIN_SCAN_INTERVAL,
    MIN_SESSION_YIELD_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"1\d{10}")
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class YorkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with the 约克智慧家 account, by password or SMS code."""

    VERSION = 2

    def __init__(self) -> None:
        self._phone: str | None = None
        self._api: YorkApi | None = None
        # Server text for the "login_rejected" error.
        self._placeholders: dict[str, str] = {"message": ""}

    def _error_key(self, err: YorkError) -> str:
        if isinstance(err, YorkLoginError):
            # The server's message says what was wrong (password, code, account...).
            self._placeholders["message"] = err.message
            return "login_rejected"
        if isinstance(err, YorkAuthError):
            return "invalid_auth"
        if isinstance(err, YorkConnectionError):
            return "cannot_connect"
        if isinstance(err, YorkClientOutdatedError):
            return "client_outdated"
        _LOGGER.warning("York login failed: %s", err)
        return "unknown"

    def _client_id(self) -> str:
        # Keep the same push id across re-authentication.
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry().data[CONF_CLIENT_ID]
        return new_client_id()

    def _make_api(self, phone: str, password: str | None = None) -> YorkApi:
        return YorkApi(
            async_get_clientsession(self.hass),
            phone,
            password=password,
            client_id=self._client_id(),
        )

    def _phone_schema(self, extra: dict | None = None) -> vol.Schema:
        default = {}
        if self.source == SOURCE_REAUTH:
            default = {"default": self._get_reauth_entry().data[CONF_PHONE]}
        return vol.Schema({vol.Required(CONF_PHONE, **default): str, **(extra or {})})

    async def _async_finish(self, api: YorkApi, password: str | None) -> ConfigFlowResult:
        assert api.auth is not None
        data = {
            CONF_PHONE: self._phone,
            CONF_PASSWORD: password,
            CONF_CLIENT_ID: api.client_id,
            CONF_SESSION: asdict(api.auth),
        }
        await self.async_set_unique_id(str(api.auth.user_id))
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"约克智慧家 {self._phone}", data=data)

    # ------------------------------------------------------------ user steps

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["password", "sms"])

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            if not _PHONE_RE.fullmatch(phone):
                errors[CONF_PHONE] = "invalid_phone"
            else:
                self._phone = phone
                api = self._make_api(phone, user_input[CONF_PASSWORD])
                try:
                    await api.login_password()
                except YorkError as err:
                    errors["base"] = self._error_key(err)
                else:
                    return await self._async_finish(api, user_input[CONF_PASSWORD])

        return self.async_show_form(
            step_id="password",
            data_schema=self._phone_schema({vol.Required(CONF_PASSWORD): _PASSWORD}),
            description_placeholders=self._placeholders,
            errors=errors,
        )

    async def async_step_sms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            if not _PHONE_RE.fullmatch(phone):
                errors[CONF_PHONE] = "invalid_phone"
            else:
                api = self._make_api(phone)
                try:
                    await api.request_sms_code()
                except YorkError as err:
                    errors["base"] = self._error_key(err)
                else:
                    self._phone, self._api = phone, api
                    return await self.async_step_sms_code()

        return self.async_show_form(
            step_id="sms",
            data_schema=self._phone_schema(),
            description_placeholders=self._placeholders,
            errors=errors,
        )

    async def async_step_sms_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._api is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._api.login_sms(user_input[CONF_VERIFY_CODE].strip())
            except YorkError as err:
                errors["base"] = self._error_key(err)
            else:
                return await self._async_finish(self._api, None)

        return self.async_show_form(
            step_id="sms_code",
            data_schema=vol.Schema({vol.Required(CONF_VERIFY_CODE): str}),
            description_placeholders={**self._placeholders, "phone": self._phone or ""},
            errors=errors,
        )

    # ---------------------------------------------------------------- reauth

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reauth", menu_options=["password", "sms"])

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return YorkOptionsFlow()


class YorkOptionsFlow(OptionsFlow):
    """Polling interval and session sharing options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Required(
                        CONF_RECLAIM_SESSION,
                        default=options.get(CONF_RECLAIM_SESSION, DEFAULT_RECLAIM_SESSION),
                    ): bool,
                    vol.Required(
                        CONF_SESSION_YIELD_MINUTES,
                        default=options.get(
                            CONF_SESSION_YIELD_MINUTES, DEFAULT_SESSION_YIELD_MINUTES
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SESSION_YIELD_MINUTES, max=MAX_SESSION_YIELD_MINUTES),
                    ),
                }
            ),
        )
