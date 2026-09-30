"""Air deflectors (导风板 / 风向) decoded and encoded like the app. No HA imports.

Both controller pages build their direction list from the constants
(service getDirectionArr) and send it with controlDirection:

* Ordinary units: ``horizontal_air_deflector_num`` deflectors (up/down),
  then ``vertical_air_deflector_num`` (left/right), numbered N = 1..4 in
  that order. Each has a level: 0 is ``position_of_air_deflectorN`` "1" (the
  modal's "auto" icon, i.e. swing), k > 0 is position "0" with
  ``section_of_air_deflectorN`` = k - 1. Every command carries every
  deflector, each with ``air_deflectorN_independent_or_together_ctrl``:
  "1" from the unified tab (all deflectors get the same level), "0" from
  the individual tab.
* 3D / far-wind units (``is_3D_wind_avalable`` / ``is_super_far_wind_avalable``
  "1"): the list is 上下风 (deflector 1), 左右风 (deflector 2), 3D出风 and
  超远风; one of them is selected and only it is sent (its position/section,
  or ``three_air_supply`` / ``ultra_far_air_supply`` "1"), without the
  unify flags.

Maximum levels per mode: getDirectionUnifyMax / getDirectionIndependentMax
for the horizontal deflectors (and 上下风), 7 for the vertical ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import is_on
from .const import (
    KEY_3D_WIND,
    KEY_DEFLECTOR_ALONE,
    KEY_DEFLECTOR_UNIFY,
    KEY_FAR_WIND,
    KEY_H_DEFLECTORS,
    KEY_THREE_AIR_SUPPLY,
    KEY_ULTRA_FAR_AIR_SUPPLY,
    KEY_V_DEFLECTORS,
    MODE_FLOOR_HEAT,
    SWING_AUTO,
    SWING_FAR,
    SWING_THREE_D,
)
from .profile import INDEPENDENT_MAX_MODES, Profile, model_str

VERTICAL_MAX = 7


@dataclass(frozen=True)
class Layout:
    """The direction list of one controller."""

    # Deflector numbers N (1-based) and whether each is vertical (left/right).
    deflectors: tuple[tuple[int, bool], ...]
    # 3D / far-wind unit: 上下风 / 左右风 plus the variants below.
    special: bool
    three_d: bool
    far: bool
    # The unified tab only (every deflector gets the same level, flag "1").
    unify_only: bool


def _count(status: dict[str, Any], key: str) -> int:
    """parseInt of the count (NaN, i.e. no deflectors, for anything else)."""
    try:
        return max(0, int(str(status.get(key)).strip()))
    except ValueError:
        return 0


def layout(status: dict[str, Any], profile: Profile) -> Layout | None:
    """getDirectionArr and the page's tab rules; None without deflectors."""
    three_d = is_on(status.get(KEY_3D_WIND))
    far = is_on(status.get(KEY_FAR_WIND))
    if three_d or far:
        # Only the individual tab (directionAllowUnifyControl = false).
        return Layout(((1, False), (2, True)), True, three_d, far, False)
    horizontal = _count(status, KEY_H_DEFLECTORS)
    vertical = _count(status, KEY_V_DEFLECTORS)
    numbers = [(n, False) for n in range(1, horizontal + 1)]
    numbers += [(n, True) for n in range(horizontal + 1, horizontal + vertical + 1)]
    # The ctrlJson keys only exist for four deflectors.
    numbers = [(n, v) for n, v in numbers if n <= 4]
    if not numbers:
        return None
    unify = is_on(status.get(KEY_DEFLECTOR_UNIFY))
    alone = is_on(status.get(KEY_DEFLECTOR_ALONE))
    if (
        profile.forces_unify
        and model_str(status.get(KEY_H_DEFLECTORS)) == "1"
        and model_str(status.get(KEY_3D_WIND)) == "0"
        and model_str(status.get(KEY_FAR_WIND)) == "0"
    ):
        # Colour page: one horizontal deflector without 3D/far wind forces
        # is_air_deflector_avalable "0" and together_ctrl "1".
        unify, alone = True, False
    # With neither tab allowed the modal keeps its default individual tab.
    return Layout(tuple(numbers), False, False, False, unify and not alone)


def direction_enabled(on: bool, mode: str | None) -> bool:
    """refreshDirectionEnable: on and not in floor heating."""
    return on and mode != MODE_FLOOR_HEAT


def level(status: dict[str, Any], number: int) -> int | None:
    """Level of deflector N: 0 for position "1", else section + 1.

    None when the section is not a number (the app would get NaN).
    """
    if model_str(status.get(f"position_of_air_deflector{number}")) == "1":
        return 0
    try:
        return int(str(status.get(f"section_of_air_deflector{number}")).strip()) + 1
    except ValueError:
        return None


def max_level(profile: Profile, lay: Layout, number: int, mode: str | None) -> int:
    """Highest fixed level of deflector N in ``mode``."""
    vertical = dict(lay.deflectors)[number]
    if vertical:
        return VERTICAL_MAX
    if lay.special:
        return 7 if mode in profile.special_independent_max_modes else 5
    if lay.unify_only:
        return 7 if mode in profile.unify_max_modes else 5
    return 7 if mode in INDEPENDENT_MAX_MODES else 5


def options(profile: Profile, lay: Layout, number: int, mode: str | None) -> list[str]:
    """Option ids of deflector N: auto (level 0) and the fixed levels."""
    return [SWING_AUTO, *(str(k) for k in range(1, max_level(profile, lay, number, mode) + 1))]


def swing_options(profile: Profile, lay: Layout, mode: str | None) -> list[str]:
    """Options of the first deflector, plus the 3D / far-wind variants."""
    result = options(profile, lay, 1, mode)
    if lay.three_d:
        result.append(SWING_THREE_D)
    if lay.far:
        result.append(SWING_FAR)
    return result


def option_level(option: str) -> int | None:
    if option == SWING_AUTO:
        return 0
    return int(option) if option.isdigit() else None


def current_option(
    status: dict[str, Any], profile: Profile, lay: Layout, number: int, mode: str | None
) -> str | None:
    """Deflector N's option; a stored level above the maximum reads as the
    maximum (the modal clamps it the same way)."""
    value = level(status, number)
    if value is None:
        return None
    return SWING_AUTO if value == 0 else str(min(value, max_level(profile, lay, number, mode)))


def current_swing(
    status: dict[str, Any], profile: Profile, lay: Layout, mode: str | None
) -> str | None:
    """getDirection: 3D出风, else 超远风, else deflector 1's level."""
    if lay.special:
        if model_str(status.get(KEY_THREE_AIR_SUPPLY)) == "1":
            return SWING_THREE_D
        if model_str(status.get(KEY_ULTRA_FAR_AIR_SUPPLY)) == "1":
            return SWING_FAR
    return current_option(status, profile, lay, 1, mode)


def _encode(number: int, value: int) -> dict[str, str]:
    if value == 0:
        return {f"position_of_air_deflector{number}": "1"}
    return {
        f"position_of_air_deflector{number}": "0",
        f"section_of_air_deflector{number}": str(value - 1),
    }


def command(
    status: dict[str, Any],
    profile: Profile,
    lay: Layout,
    mode: str | None,
    number: int,
    option: str,
) -> dict[str, str]:
    """controlDirection's ctrlJson for setting deflector N (or a variant)."""
    if lay.special:
        if option == SWING_THREE_D:
            return {KEY_THREE_AIR_SUPPLY: "1"}
        if option == SWING_FAR:
            return {KEY_ULTRA_FAR_AIR_SUPPLY: "1"}
        return _encode(number, option_level(option) or 0)
    new = option_level(option) or 0
    cmd: dict[str, str] = {}
    if lay.unify_only:
        for n, _ in lay.deflectors:
            cmd.update(_encode(n, new))
            cmd[f"air_deflector{n}_independent_or_together_ctrl"] = "1"
        return cmd
    for n, _ in lay.deflectors:
        if n == number:
            value: int | None = new
        elif (value := level(status, n)) is not None:
            # The modal clamps the other deflectors to their maximum.
            value = min(value, max_level(profile, lay, n, mode))
        if value is not None:
            # A deflector whose level cannot be read is left out rather
            # than moved (the app would send "NaN").
            cmd.update(_encode(n, value))
        cmd[f"air_deflector{n}_independent_or_together_ctrl"] = "0"
    return cmd


def close_far(status: dict[str, Any], lay: Layout | None) -> dict[str, str]:
    """closeFarDirection: leaving 超远风 selects 上下风 at its level again."""
    if lay is None or not lay.special or not current_swing_is_far(status):
        return {}
    value = level(status, 1)
    return {} if value is None else _encode(1, value)


def current_swing_is_far(status: dict[str, Any]) -> bool:
    """getDirection's type is FAR: ultra-far selected and not 3D."""
    return (
        model_str(status.get(KEY_THREE_AIR_SUPPLY)) != "1"
        and model_str(status.get(KEY_ULTRA_FAR_AIR_SUPPLY)) == "1"
    )


def implied_state(lay: Layout | None, cmd: dict[str, Any]) -> dict[str, Any]:
    """Local state a direction command implies on a 3D / far-wind unit.

    The page selects exactly one entry, so choosing 上下风 / 左右风 / 3D / far
    deselects the others; it never sends the "0" for them, so HA only shows
    it locally (and re-reads the full state after the grace period).
    """
    implied: dict[str, Any] = {}
    if lay is None or not lay.special:
        return implied
    if KEY_THREE_AIR_SUPPLY in cmd:
        implied[KEY_ULTRA_FAR_AIR_SUPPLY] = "0"
    elif KEY_ULTRA_FAR_AIR_SUPPLY in cmd:
        implied[KEY_THREE_AIR_SUPPLY] = "0"
    elif any(k.startswith("position_of_air_deflector") for k in cmd):
        implied[KEY_THREE_AIR_SUPPLY] = "0"
        implied[KEY_ULTRA_FAR_AIR_SUPPLY] = "0"
    return implied
