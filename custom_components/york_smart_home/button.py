"""Filter reset button for York wire controllers."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .control import filter_reset_command, filter_reset_state, has_filter_tips
from .coordinator import YorkConfigEntry
from .entity import YorkEntity, async_add_device_entities

PARALLEL_UPDATES = 0  # the coordinator serializes commands per controller


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, code: (
            [YorkFilterReset(coordinator, code)]
            if has_filter_tips(coordinator.devices[code].view)
            else []
        ),
        wait_for_status=True,
    )


class YorkFilterReset(YorkEntity, ButtonEntity):
    """滤网复位: tell the controller the filter has been cleaned.

    Same request as the control page's filterScreenReset (colour AIR
    controllers) and its filter dialog (fresh-air controllers): ctrlJson
    {"screen_mark_reset": "1"} through the normal single-control path.
    The app offers it from the filter banner; HA allows it at any time,
    resetting a counter that is not due does nothing harmful.
    """

    _attr_translation_key = "filter_reset"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, code: str) -> None:
        super().__init__(coordinator, code, "filter_reset")

    async def async_press(self) -> None:
        await self.coordinator.async_send_raw(
            self._code,
            lambda view: filter_reset_command(),
            # The app hides its filter banner until the next status shows
            # it again; HA clears the reminder and holds that for the grace
            # period, then reads the full state once (recheck) so the
            # controller's own tips decide, even when its incremental
            # reports leave them out.
            optimistic=lambda view, cmd: filter_reset_state(view, self.device.profile),
            recheck=True,
        )
