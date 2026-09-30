"""Air deflectors (风向), checked against getDirectionArr / controlDirection."""

from __future__ import annotations

import pytest

from custom_components.york_smart_home import deflectors
from custom_components.york_smart_home.const import MODES
from custom_components.york_smart_home.control import CommandRefused, build_command, command_state
from custom_components.york_smart_home.profile import COLOR, STANDARD

from .conftest import FIXTURE


def _status(**overrides) -> dict:
    return {**FIXTURE["device"]["constantJson"], **FIXTURE["status"], **overrides}


def _mode(status: dict, mode: str) -> dict:
    status = dict(status)
    for key, _ in MODES.values():
        status[key] = "0"
    status[MODES[mode][0]] = "1"
    return status


# Two horizontal and one vertical deflector, individual control allowed.
THREE = {
    "horizontal_air_deflector_num": "2",
    "vertical_air_deflector_num": "1",
    "is_air_deflector_avalable": "1",
    "air_deflector_independent_or_together_ctrl": "1",
    "position_of_air_deflector1": "0",
    "section_of_air_deflector1": "2",  # level 3
    "position_of_air_deflector2": "1",  # auto
    "section_of_air_deflector2": "0",
    "position_of_air_deflector3": "0",
    "section_of_air_deflector3": "6",  # level 7
}


def _three(**overrides) -> dict:
    return _status(**{**THREE, **overrides})


def test_user_device_has_no_deflectors() -> None:
    assert deflectors.layout(_status(), COLOR) is None
    with pytest.raises(CommandRefused) as err:
        build_command(_status(), {"direction": (1, "auto")})
    assert err.value.key == "swing_not_supported"


def test_layout_and_levels() -> None:
    status = _three()
    lay = deflectors.layout(status, COLOR)
    assert lay.deflectors == ((1, False), (2, False), (3, True))
    assert not lay.special and not lay.unify_only
    assert [deflectors.level(status, n) for n in (1, 2, 3)] == [3, 0, 7]
    assert deflectors.level(_three(section_of_air_deflector1="x"), 1) is None


@pytest.mark.parametrize(
    ("mode", "independent", "unify"),
    [
        # getDirectionIndependentMax (no 3D) / getDirectionUnifyMax.
        ("cool", 7, 5),
        ("heat", 7, 7),
        ("fan_only", 7, 7),
        ("dry", 7, 5),
        ("auto", 7, 5),
        ("dry_auto", 7, 5),
        ("heat_floor_heat", 7, 7),
        ("fan_floor_heat", 5, 7),
        ("heat_supply", 5, 7),
        ("dry_comfort", 5, 5),
        ("purify", 5, 5),
        (None, 5, 5),
    ],
)
def test_max_levels(mode, independent, unify) -> None:
    status = _three()
    lay = deflectors.layout(status, COLOR)
    assert deflectors.max_level(COLOR, lay, 1, mode) == independent
    assert deflectors.max_level(COLOR, lay, 3, mode) == 7  # vertical
    unified = deflectors.layout(_three(is_air_deflector_avalable="0"), COLOR)
    assert unified.unify_only
    assert deflectors.max_level(COLOR, unified, 1, mode) == unify


def test_standard_unify_max_has_no_heat_supply() -> None:
    status = _three(is_air_deflector_avalable="0")
    lay = deflectors.layout(status, STANDARD)
    assert deflectors.max_level(STANDARD, lay, 1, "heat_supply") == 5
    assert deflectors.max_level(COLOR, lay, 1, "heat_supply") == 7


def test_independent_command_sends_every_deflector() -> None:
    status = _three()
    cmd = build_command(status, {"direction": (2, "4")})
    assert cmd == {
        "position_of_air_deflector1": "0",
        "section_of_air_deflector1": "2",
        "air_deflector1_independent_or_together_ctrl": "0",
        "position_of_air_deflector2": "0",
        "section_of_air_deflector2": "3",
        "air_deflector2_independent_or_together_ctrl": "0",
        "position_of_air_deflector3": "0",
        "section_of_air_deflector3": "6",
        "air_deflector3_independent_or_together_ctrl": "0",
    }
    # Level 0 (auto) has no section key.
    cmd = build_command(status, {"direction": (1, "auto")})
    assert cmd["position_of_air_deflector1"] == "1"
    assert "section_of_air_deflector1" not in cmd


def test_independent_command_clamps_other_deflectors() -> None:
    # In fan_floor_heat the horizontal maximum is 5: the modal clamps 7 to 5.
    status = _mode(_three(section_of_air_deflector1="6"), "fan_floor_heat")
    cmd = build_command(status, {"direction": (3, "2")})
    assert cmd["section_of_air_deflector1"] == "4"
    assert cmd["section_of_air_deflector3"] == "1"


def test_unreadable_deflector_left_out() -> None:
    status = _three(section_of_air_deflector1=None)
    cmd = build_command(status, {"direction": (2, "1")})
    assert "position_of_air_deflector1" not in cmd
    assert cmd["air_deflector1_independent_or_together_ctrl"] == "0"


def test_unified_command() -> None:
    status = _three(is_air_deflector_avalable="0")
    cmd = build_command(status, {"direction": (1, "5")})
    for n in (1, 2, 3):
        assert cmd[f"position_of_air_deflector{n}"] == "0"
        assert cmd[f"section_of_air_deflector{n}"] == "4"
        assert cmd[f"air_deflector{n}_independent_or_together_ctrl"] == "1"
    # Unify max in cool mode is 5.
    with pytest.raises(CommandRefused) as err:
        build_command(status, {"direction": (1, "6")})
    assert err.value.key == "unsupported_swing_mode"


def test_single_horizontal_deflector_forces_unify_on_colour_page() -> None:
    status = _status(
        horizontal_air_deflector_num="1",
        is_air_deflector_avalable="1",
        air_deflector_independent_or_together_ctrl="0",
    )
    assert deflectors.layout(status, COLOR).unify_only
    # The standard page has no such rule.
    assert not deflectors.layout(status, STANDARD).unify_only
    cmd = build_command(status, {"direction": (1, "auto")})
    assert cmd == {
        "position_of_air_deflector1": "1",
        "air_deflector1_independent_or_together_ctrl": "1",
    }


def test_neither_tab_allowed_keeps_individual() -> None:
    status = _three(is_air_deflector_avalable="0")
    status["air_deflector_independent_or_together_ctrl"] = "0"
    assert not deflectors.layout(status, COLOR).unify_only


SPECIAL = {
    "is_3D_wind_avalable": "1",
    "is_super_far_wind_avalable": "1",
    "position_of_air_deflector1": "0",
    "section_of_air_deflector1": "1",
    "position_of_air_deflector2": "1",
}


def test_3d_far_layout() -> None:
    status = _status(**SPECIAL)
    lay = deflectors.layout(status, COLOR)
    assert lay.special and lay.three_d and lay.far and not lay.unify_only
    assert deflectors.swing_options(COLOR, lay, "cool") == [
        "auto",
        "1",
        "2",
        "3",
        "4",
        "5",
        "three_d",
        "far",
    ]
    # 3D units: 7 levels only in HOT/WIND/HOT_FLOOR/HEATING.
    assert deflectors.max_level(COLOR, lay, 1, "heat_supply") == 7
    assert deflectors.max_level(STANDARD, lay, 1, "heat_supply") == 5
    assert deflectors.options(COLOR, lay, 2, "cool")[-1] == "7"
    assert deflectors.current_swing(status, COLOR, lay, "cool") == "2"
    assert (
        deflectors.current_swing({**status, "ultra_far_air_supply": "1"}, COLOR, lay, "cool")
        == "far"
    )
    both = {**status, "ultra_far_air_supply": "1", "three_air_supply": "1"}
    assert deflectors.current_swing(both, COLOR, lay, "cool") == "three_d"


@pytest.mark.parametrize(
    ("number", "option", "expected", "implied"),
    [
        (1, "three_d", {"three_air_supply": "1"}, {"ultra_far_air_supply": "0"}),
        (1, "far", {"ultra_far_air_supply": "1"}, {"three_air_supply": "0"}),
        (
            1,
            "4",
            {"position_of_air_deflector1": "0", "section_of_air_deflector1": "3"},
            {"three_air_supply": "0", "ultra_far_air_supply": "0"},
        ),
        (
            2,
            "auto",
            {"position_of_air_deflector2": "1"},
            {"three_air_supply": "0", "ultra_far_air_supply": "0"},
        ),
    ],
)
def test_3d_far_commands(number, option, expected, implied) -> None:
    """Only the selected entry is sent, without the unify flags."""
    status = _status(**SPECIAL)
    cmd = build_command(status, {"direction": (number, option)})
    assert cmd == expected
    assert command_state(status, cmd) == {**expected, **implied}


def test_direction_disabled_when_off_or_floor_heating() -> None:
    for status in (
        _three(if_run="0"),
        _mode(_three(), "floor_heat"),
    ):
        with pytest.raises(CommandRefused) as err:
            build_command(status, {"direction": (1, "auto")})
        assert err.value.key == "swing_not_available"


def test_direction_cancels_functions_and_one_direction() -> None:
    status = _status(
        **THREE,
        is_nosense_wind_avalable="1",
        no_sense_of_wind="1",
        is_health_avalable="1",
        health="1",
        one_direction_human_sense="1",
    )
    cmd = build_command(status, {"direction": (1, "auto")})
    assert cmd["no_sense_of_wind"] == "0" and cmd["health"] == "1"
    assert cmd["one_direction_human_sense"] == "0"
    assert cmd["position_of_air_deflector1"] == "1"
    # Standard page: NO_WIND and FOREST only, no human sensing close.
    cmd = build_command(status, {"direction": (1, "auto")}, STANDARD)
    assert cmd["no_sense_of_wind"] == "0"
    assert "one_direction_human_sense" not in cmd


def test_plain_command_state_implies_nothing() -> None:
    status = _three()
    cmd = build_command(status, {"direction": (1, "2")})
    assert command_state(status, cmd) == cmd
