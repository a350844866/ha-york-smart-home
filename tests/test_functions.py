"""Extra functions, human sensing and humidity, checked against the app's pages.

xkq-color-control (chunk 41) for the colour controller, xkq-control
(chunk 44) for the standard controller (xkqType 8). The tables below are
transcribed from the app's code with its own ids, independently of
profile.py, and compared with it.
"""

from __future__ import annotations

import pytest

from custom_components.york_smart_home.const import MODES
from custom_components.york_smart_home.control import (
    CommandRefused,
    build_command,
    command_state,
    current_humidity_setpoint,
    functions_enabled,
    humidity_control,
    k3_settings,
    one_direction_options,
    supported_fans,
    temp_limits,
)
from custom_components.york_smart_home.profile import (
    COLOR,
    FUNC_KEYS,
    STANDARD,
    SWITCH_FUNCS,
    active_funcs,
    func_enabled,
    offered_funcs,
    profile_for,
)

from .conftest import FIXTURE

# The app's function ids (8pph / aDRB enums) -> the integration's ids.
APP_IDS = {
    "SAVE": "save",
    "SLEEP": "sleep",
    "MUTE": "mute",
    "HEALTH": "health",
    "FOREST": "forest",
    "HOT": "aux_heat",
    "CLEAN": "clean",
    "STRONG": "strong",
    "NO_WIND": "no_wind",
    "PEOPLE_ONE": "people_one",
    "PEOPLE": "people",
    "LEAVE": "leave",
    "HIGH_TEMP_STERILIZE": "sterilize",
    "HUMI": "humidify",
    "SMART_PEOPLE_FEELING": "smart_people",
    "WIND_BLOW_HUM": "wind_blow",
    "WIND_SHELTER_HUM": "wind_shelter",
}

# funcsChanged of chunk 41 (colour page), case by case.
COLOR_APP_EXCLUSIONS = {
    "SAVE": "MUTE SLEEP HOT FOREST NO_WIND LEAVE STRONG SMART_PEOPLE_FEELING CLEAN "
    "HIGH_TEMP_STERILIZE",
    "SLEEP": "SAVE MUTE FOREST NO_WIND LEAVE STRONG SMART_PEOPLE_FEELING CLEAN HIGH_TEMP_STERILIZE",
    "MUTE": "SAVE SLEEP FOREST NO_WIND LEAVE STRONG SMART_PEOPLE_FEELING CLEAN HIGH_TEMP_STERILIZE",
    "HEALTH": "LEAVE CLEAN HIGH_TEMP_STERILIZE",
    "HOT": "SAVE FOREST NO_WIND LEAVE SMART_PEOPLE_FEELING CLEAN HIGH_TEMP_STERILIZE",
    "FOREST": "SAVE MUTE SLEEP HOT NO_WIND LEAVE STRONG SMART_PEOPLE_FEELING WIND_BLOW_HUM "
    "WIND_SHELTER_HUM CLEAN HIGH_TEMP_STERILIZE",
    "NO_WIND": "SAVE MUTE SLEEP HOT FOREST LEAVE STRONG PEOPLE PEOPLE_ONE SMART_PEOPLE_FEELING "
    "WIND_BLOW_HUM WIND_SHELTER_HUM CLEAN HIGH_TEMP_STERILIZE",
    "LEAVE": "SAVE MUTE SLEEP HEALTH HOT FOREST NO_WIND STRONG PEOPLE PEOPLE_ONE "
    "SMART_PEOPLE_FEELING WIND_BLOW_HUM WIND_SHELTER_HUM CLEAN HIGH_TEMP_STERILIZE",
    "STRONG": "SAVE MUTE SLEEP FOREST NO_WIND LEAVE SMART_PEOPLE_FEELING CLEAN HIGH_TEMP_STERILIZE",
    # humanFeelChanged / oneHumanFeelChanged (the funcsChanged case is dead).
    "PEOPLE": "CLEAN HIGH_TEMP_STERILIZE NO_WIND",
    "PEOPLE_ONE": "CLEAN HIGH_TEMP_STERILIZE NO_WIND",
    # showSmartPeopleFeelingModal's list after "confirm".
    "SMART_PEOPLE_FEELING": "SAVE MUTE SLEEP HOT FOREST NO_WIND LEAVE STRONG WIND_BLOW_HUM "
    "WIND_SHELTER_HUM CLEAN HIGH_TEMP_STERILIZE",
    "WIND_BLOW_HUM": "FOREST NO_WIND LEAVE SMART_PEOPLE_FEELING WIND_SHELTER_HUM CLEAN "
    "HIGH_TEMP_STERILIZE",
    "WIND_SHELTER_HUM": "FOREST NO_WIND LEAVE SMART_PEOPLE_FEELING WIND_BLOW_HUM CLEAN "
    "HIGH_TEMP_STERILIZE",
    "CLEAN": "SAVE MUTE SLEEP HEALTH HOT FOREST NO_WIND LEAVE STRONG PEOPLE PEOPLE_ONE "
    "SMART_PEOPLE_FEELING WIND_BLOW_HUM WIND_SHELTER_HUM HIGH_TEMP_STERILIZE",
    "HIGH_TEMP_STERILIZE": "SAVE MUTE SLEEP HEALTH HOT FOREST NO_WIND LEAVE STRONG PEOPLE "
    "PEOPLE_ONE SMART_PEOPLE_FEELING WIND_BLOW_HUM WIND_SHELTER_HUM CLEAN",
    "HUMI": "",
}
# funcsChanged of chunk 44 (standard page); LEAVE has no case.
STANDARD_APP_EXCLUSIONS = {
    "SAVE": "SLEEP MUTE FOREST HOT CLEAN STRONG NO_WIND HIGH_TEMP_STERILIZE",
    "SLEEP": "SAVE MUTE FOREST CLEAN STRONG NO_WIND HIGH_TEMP_STERILIZE",
    "MUTE": "SAVE SLEEP FOREST CLEAN STRONG NO_WIND HIGH_TEMP_STERILIZE",
    "HEALTH": "CLEAN HIGH_TEMP_STERILIZE",
    "FOREST": "SAVE SLEEP MUTE HOT CLEAN STRONG NO_WIND HIGH_TEMP_STERILIZE",
    "HOT": "CLEAN NO_WIND SAVE FOREST HIGH_TEMP_STERILIZE",
    "CLEAN": "SAVE SLEEP MUTE HEALTH FOREST HOT STRONG NO_WIND PEOPLE HIGH_TEMP_STERILIZE",
    "STRONG": "SAVE SLEEP MUTE FOREST CLEAN NO_WIND HIGH_TEMP_STERILIZE",
    "NO_WIND": "SAVE SLEEP MUTE FOREST HOT CLEAN STRONG HIGH_TEMP_STERILIZE PEOPLE",
    "PEOPLE": "CLEAN HIGH_TEMP_STERILIZE NO_WIND",
    "HIGH_TEMP_STERILIZE": "SAVE SLEEP MUTE HEALTH FOREST HOT STRONG CLEAN NO_WIND PEOPLE",
    "LEAVE": "",
}


def _ids(names: str) -> frozenset[str]:
    return frozenset(APP_IDS[n] for n in names.split())


@pytest.mark.parametrize(
    ("profile", "table"),
    [(COLOR, COLOR_APP_EXCLUSIONS), (STANDARD, STANDARD_APP_EXCLUSIONS)],
    ids=["color", "standard"],
)
def test_exclusion_tables_match_app(profile, table) -> None:
    assert {APP_IDS[k]: _ids(v) for k, v in table.items()} == profile.exclusions


def test_function_table_matches_app() -> None:
    # q8O6 allFuncArr (chunk 3): statusKey / controlKey, in the app's order.
    assert list(FUNC_KEYS.items()) == [
        ("save", ("energy_conservation", "is_energy_conservation_avalable")),
        ("sleep", ("sleep", "is_sleep_avalable")),
        ("mute", ("indoor_unit_silent", "is_indoor_unit_silent_avalable")),
        ("health", ("health", "is_health_avalable")),
        ("forest", ("forest_wind", "is_forest_wind_avalable")),
        ("aux_heat", ("auxiliary_heat", "is_auxiliary_heat_avalable")),
        ("clean", ("self_clean", "is_self_clean_avalable")),
        ("strong", ("force_switch", "is_force_avalable")),
        ("no_wind", ("no_sense_of_wind", "is_nosense_wind_avalable")),
        ("people_one", ("one_direction_human_sense_effect", "has_people_one")),
        ("people", ("independent_human_sense_effect", "has_people_independent_K3")),
        ("leave", ("away_home_energy_saving", "is_awayhome_saveenergy_avalable")),
        ("sterilize", ("hig_temperature_sterilization", "hig_temperature_sterilization_fix")),
        ("humidify", ("humidification", "is_humidification_avalable")),
        ("smart_people", ("human_sensing_control_3d", "has_smart_people_feeling")),
        ("wind_blow", ("one_direction_human_sense", "has_smart_people_feeling")),
        ("wind_shelter", ("one_direction_human_sense", "has_smart_people_feeling")),
    ]
    # yNUN allFuncArr (standard): no HUMI / SMART / PEOPLE_ONE / 3D toggles.
    assert STANDARD.funcs == (
        "save",
        "sleep",
        "mute",
        "health",
        "forest",
        "aux_heat",
        "clean",
        "strong",
        "no_wind",
        "people",
        "leave",
        "sterilize",
    )


# ------------------------------------------------------------------ fixtures


def _status(**overrides) -> dict:
    return {**FIXTURE["device"]["constantJson"], **FIXTURE["status"], **overrides}


REGULAR = [f for f in SWITCH_FUNCS if f != "smart_people"]
ALL_AVAILABLE = {FUNC_KEYS[f][1]: "1" for f in REGULAR}


def _with(profile_funcs=REGULAR, **overrides) -> dict:
    """Fixture state (on, cool, 27.5, low fan) offering ``profile_funcs``."""
    status = _status(**{FUNC_KEYS[f][1]: "1" for f in profile_funcs})
    return {**status, **overrides}


def _mode(status: dict, mode: str) -> dict:
    status = dict(status)
    for key, _ in MODES.values():
        status[key] = "0"
    status[MODES[mode][0]] = "1"
    return status


def test_user_device_offers_no_function() -> None:
    assert offered_funcs(_status(), COLOR) == []
    assert functions_enabled(_status()) == {}
    assert profile_for("9") is COLOR
    assert profile_for("8") is STANDARD


def test_offered_color_needs_string_one() -> None:
    status = _with()
    assert offered_funcs(status, COLOR) == REGULAR
    assert offered_funcs(_status(is_sleep_avalable=1), COLOR) == ["sleep"]
    # "2" is not "1" for the colour page ...
    assert offered_funcs(_status(is_sleep_avalable="2"), COLOR) == []


def test_offered_standard_is_anything_but_zero() -> None:
    # ... but the standard page offers everything that is not "0".
    assert offered_funcs(_status(is_sleep_avalable="2"), STANDARD) == ["sleep"]
    assert offered_funcs(_status(is_sleep_avalable=0), STANDARD) == []
    assert offered_funcs(_status(is_sleep_avalable=""), STANDARD) == []
    status = {k: v for k, v in _status().items() if k != "is_sleep_avalable"}
    assert offered_funcs(status, STANDARD) == []
    # Human sensing: statusKey human_sense_type (any type).
    assert offered_funcs(_status(human_sense_type="1"), STANDARD) == ["people"]
    # No humidification / smart sensing on the standard page.
    status = _status(is_humidification_avalable="1", human_sense_type2="2")
    assert offered_funcs(status, STANDARD) == []


def test_derived_human_sensing_flags() -> None:
    assert offered_funcs(_status(human_sense_type="1"), COLOR) == ["people_one"]
    assert offered_funcs(_status(human_sense_type="2"), COLOR) == ["people"]
    assert offered_funcs(_status(human_sense_type="3"), COLOR) == ["people"]
    smart = ["smart_people", "wind_blow", "wind_shelter"]
    assert offered_funcs(_status(human_sense_type2="2"), COLOR) == smart
    # has_smart_people_feeling also needs no temperature / fan lock.
    assert offered_funcs(_status(human_sense_type2="2", set_temp_fixed="1"), COLOR) == []
    assert offered_funcs(_status(human_sense_type2="2", B9="1"), COLOR) == []


# --------------------------------------------------------------- enable rules

FLOORS = {"floor_heat", "dry_floor_heat", "heat_floor_heat", "fan_floor_heat"}


def _app_enabled(profile, func, on, mode, temp_fixed=False, fan_fixed=False, tx=False):
    """refreshFuncEnable, transcribed from chunks 41 / 44."""
    color = profile is COLOR
    clear = {"purify"} if color else set()
    if func in ("save", "sleep"):
        blocked = {"fan_only", "auto"} | FLOORS | clear
        return not (temp_fixed or fan_fixed) and on and mode not in blocked
    if func == "mute":
        return not fan_fixed and on and mode not in {"auto"} | FLOORS | clear
    if func == "health":
        return on and mode not in FLOORS | clear
    if func == "forest":
        return on and mode == "cool"
    if func == "aux_heat":
        return on and mode == "heat"
    if func == "clean":
        return not on and mode not in FLOORS | clear
    if func == "strong":
        return on and mode in ("cool", "heat")
    if func == "no_wind":
        return not fan_fixed and on and mode not in FLOORS | {"auto"} | clear
    if func in ("people", "people_one", "wind_blow", "wind_shelter"):
        return on and not (color and tx) and mode not in FLOORS | clear
    if func == "leave":
        return on and mode == "floor_heat" and (color or not temp_fixed)
    if func == "sterilize":
        return not on and mode not in FLOORS | clear | ({"dry_auto"} if color else set())
    # HUMI falls through to SMART_PEOPLE_FEELING (colour page only).
    return on and not (color and tx) and mode not in {"dry_auto"} | FLOORS | clear


ENABLE_FUNCS = [*REGULAR, "smart_people", "people", "people_one", "wind_blow", "wind_shelter"]


@pytest.mark.parametrize("profile", [COLOR, STANDARD], ids=["color", "standard"])
@pytest.mark.parametrize("func", ENABLE_FUNCS)
def test_enable_rules(profile, func) -> None:
    for mode in [*MODES, None]:
        for on in (True, False):
            for temp_fixed, fan_fixed, tx in [
                (False, False, False),
                (True, False, False),
                (False, True, False),
                (False, False, True),
            ]:
                status = _status(
                    set_temp_fixed="1" if temp_fixed else "0",
                    B9="1" if fan_fixed else "0",
                    communication_exception="1" if tx else "0",
                    human_sense_type2="2" if tx else "0",
                )
                expected = _app_enabled(profile, func, on, mode, temp_fixed, fan_fixed, tx)
                got = func_enabled(status, profile, func, on=on, mode=mode)
                assert got is expected, (func, mode, on, temp_fixed, fan_fixed, tx)


def test_disabled_function_is_refused() -> None:
    status = _with()
    with pytest.raises(CommandRefused) as err:
        build_command(status, {"funcs": {"aux_heat": True}})  # heating only
    assert err.value.key == "function_not_available"
    assert err.value.placeholders == {"function": "aux_heat"}
    with pytest.raises(CommandRefused) as err:
        build_command(_status(), {"funcs": {"sleep": True}})
    assert err.value.key == "function_not_supported"
    # Already in the requested state: nothing to refuse, nothing to send.
    assert build_command(_with(), {"funcs": {"aux_heat": False}}) == {}


# ------------------------------------------------------------ funcsChanged


def _flags(cmd: dict, funcs=REGULAR) -> dict:
    return {f: cmd[FUNC_KEYS[f][0]] for f in funcs if FUNC_KEYS[f][0] in cmd}


def test_turning_on_sends_every_flag_with_types() -> None:
    cmd = build_command(_with(), {"funcs": {"save": True}})
    assert cmd == {
        "energy_conservation": "1",
        "sleep": "0",
        "indoor_unit_silent": "0",
        "health": "0",
        "forest_wind": "0",
        "auxiliary_heat": "0",
        "self_clean": 0,
        "force_switch": "0",
        "no_sense_of_wind": "0",
        "away_home_energy_saving": "0",
        "hig_temperature_sterilization": 0,
        "humidification": "0",
    }


def _state_for(func: str, profile) -> dict:
    """A state in which ``func`` can be turned on, every other function on."""
    funcs = [f for f in REGULAR if f in profile.funcs]
    status = _with(funcs, human_sense_type="2")
    status.update({FUNC_KEYS[f][0]: "1" for f in funcs if f != func})
    status.update(self_clean=0 if func == "clean" else 1)
    status.update(hig_temperature_sterilization=0 if func == "sterilize" else 1)
    status.update(independent_human_sense_effect="1")
    status[FUNC_KEYS[func][0]] = 0 if func in ("clean", "sterilize") else "0"
    mode = {"aux_heat": "heat", "leave": "floor_heat"}.get(func, "cool")
    status = _mode(status, mode)
    if func in ("clean", "sterilize"):
        status["if_run"] = "0"
    return status


@pytest.mark.parametrize("profile", [COLOR, STANDARD], ids=["color", "standard"])
@pytest.mark.parametrize("func", [f for f in REGULAR if f != "humidify"])
def test_exclusions_applied(profile, func) -> None:
    if func not in profile.funcs:
        pytest.skip("not on this page")
    status = _state_for(func, profile)
    cmd = build_command(status, {"funcs": {func: True}}, profile)
    excluded = profile.exclusions[func]
    for other in (f for f in REGULAR if f in profile.funcs):
        value = cmd[FUNC_KEYS[other][0]]
        on = other == func or other not in excluded
        if other == "clean":
            assert value == (1 if on else 0), other
        elif other == "sterilize":
            assert value == (1 if on else profile.sterilize_off), other
        else:
            assert value == ("1" if on else "0"), other
    # The K3 human sensing is switched off (its three keys sent) when excluded.
    if "people" in excluded:
        assert cmd["independent_human_sense_effect"] == "0"
        assert cmd["independent_human_sense_state"] == "1"
        assert cmd["independent_human_sense_nobody_judge_time"] == "1"
    elif profile is COLOR:
        assert "independent_human_sense_effect" not in cmd
    else:
        # The standard page sends PEOPLE's flag with the others.
        assert cmd["independent_human_sense_effect"] == "1"


def test_humidification_has_no_exclusions() -> None:
    status = _with(energy_conservation="1", sleep="1")
    cmd = build_command(status, {"funcs": {"humidify": True}})
    assert _flags(cmd)["humidify"] == "1"
    assert _flags(cmd)["save"] == "1" and _flags(cmd)["sleep"] == "1"


def test_turning_off_only_clears_itself() -> None:
    status = _with(energy_conservation="1", health="1")
    cmd = build_command(status, {"funcs": {"health": False}})
    assert _flags(cmd)["save"] == "1"
    assert _flags(cmd)["health"] == "0"


@pytest.mark.parametrize(
    ("fans", "expected"),
    [
        (["low", "medium", "high"], "wire_controller_wind_low"),
        (["super_low", "low", "medium", "high"], "wire_controller_wind_low"),
        (
            ["silent", "super_low", "low", "medium", "high", "super_high"],
            "wire_controller_wind_light",
        ),
        (
            ["silent", "super_low", "low", "medium", "high", "super_high", "auto"],
            "wire_controller_wind_light",
        ),
        (["low", "medium", "high", "super_high", "auto"], None),
        (["medium", "high", "auto"], None),  # low missing: the app would fail
    ],
)
def test_sleep_forces_fan(fans, expected) -> None:
    from custom_components.york_smart_home.const import FAN_MODES

    status = _with()
    for fan, (control, support) in FAN_MODES.items():
        status[support] = "1" if fan in fans else "0"
        status[control] = "0"
    status["wire_controller_wind_high"] = "1"
    cmd = build_command(status, {"funcs": {"sleep": True}})
    fan_keys = {k for k in cmd if k.startswith("wire_controller_wind_")}
    assert fan_keys == ({expected} if expected else set())
    if expected:
        assert cmd[expected] == "1"
        # Not a fan change of the user: SAVE etc. are not cancelled by it.
        assert "energy_conservation" in cmd


def test_sleep_fan_with_six_speeds_also_on_standard() -> None:
    from custom_components.york_smart_home.const import FAN_MODES

    status = _with(STANDARD.funcs[:1] + ("sleep",))
    for control, support in FAN_MODES.values():
        status[support] = "1"
        status[control] = "0"
    status["wire_controller_wind_auto_fix"] = "0"
    status["wire_controller_wind_high"] = "1"
    cmd = build_command(status, {"funcs": {"sleep": True}}, STANDARD)
    assert cmd["wire_controller_wind_light"] == "1"


def test_sleep_side_effect_repeats_on_any_change() -> None:
    # funcsChanged re-applies it whenever SLEEP stays selected.
    status = _with(sleep="1", wire_controller_wind_low="0", wire_controller_wind_high="1")
    cmd = build_command(status, {"funcs": {"health": True}})
    assert cmd["wire_controller_wind_low"] == "1"


@pytest.mark.parametrize(
    ("heat_down", "expected"),
    [(16, {"set_temp": 19, "set_temperature_add05": "0"}), (22, {"set_temp": 22})],
)
def test_leave_sets_temperature(heat_down, expected) -> None:
    status = _mode(_with(heat_down_value=heat_down), "floor_heat")
    status.update(set_temp=25, set_temperature_add05="0")
    cmd = build_command(status, {"funcs": {"leave": True}})
    assert cmd["away_home_energy_saving"] == "1"
    for key, value in expected.items():
        assert cmd[key] == value


def test_leave_temperature_not_sent_when_unchanged() -> None:
    status = _mode(_with(heat_down_value=16), "floor_heat")
    status.update(set_temp=19, set_temperature_add05="0")
    cmd = build_command(status, {"funcs": {"leave": True}})
    assert "set_temp" not in cmd


@pytest.mark.parametrize("func", ["clean", "sterilize"])
def test_clean_and_sterilize_start_with_power_on(func) -> None:
    status = _with(if_run="0", energy_conservation="1")
    cmd = build_command(status, {"funcs": {func: True}})
    key = FUNC_KEYS[func][0]
    assert cmd["if_run"] == "1"
    assert cmd[key] == 1 and isinstance(cmd[key], int)
    other = "hig_temperature_sterilization" if func == "clean" else "self_clean"
    assert cmd[other] == 0
    assert cmd["energy_conservation"] == "0"


@pytest.mark.parametrize(
    ("func", "running", "expected"),
    [
        ("clean", {"self_clean": 1}, {"self_clean": 0, "if_run": "0"}),
        (
            "sterilize",
            {"hig_temperature_sterilization": 1},
            {"hig_temperature_sterilization": 0, "if_run": "0"},
        ),
        ("clean", {"self_clean": 0}, {}),
        ("clean", {"self_clean": 2}, {}),  # cooling down: nothing to stop
    ],
)
@pytest.mark.parametrize("profile", [COLOR, STANDARD], ids=["color", "standard"])
def test_stopping_clean_and_sterilize(func, running, expected, profile) -> None:
    """The overlay's stop button (showSelfCleanModal close), both pages."""
    status = _with(**running)
    assert build_command(status, {"funcs": {func: False}}, profile) == expected


def test_standard_sterilize_off_is_a_string() -> None:
    status = _with(STANDARD.funcs, energy_conservation="0")
    cmd = build_command(status, {"funcs": {"save": True}}, STANDARD)
    assert cmd["hig_temperature_sterilization"] == "0"
    assert cmd["self_clean"] == 0


# ------------------------------------------------------- cancellations


@pytest.mark.parametrize(
    ("profile", "change", "cancelled"),
    [
        (COLOR, {"temperature": 26.0}, {"save", "smart_people"}),
        (COLOR, {"fan": "high"}, {"save", "mute", "forest", "strong", "no_wind", "smart_people"}),
        (STANDARD, {"temperature": 26.0}, {"save"}),
        (STANDARD, {"fan": "high"}, {"save", "mute", "forest", "strong", "no_wind"}),
    ],
    ids=["color-temp", "color-fan", "standard-temp", "standard-fan"],
)
def test_cancel_lists(profile, change, cancelled) -> None:
    funcs = [f for f in SWITCH_FUNCS if f in profile.funcs]
    status = _with(funcs, human_sense_type2="2")
    status.update({FUNC_KEYS[f][0]: "1" for f in funcs})
    status.update(self_clean=0, hig_temperature_sterilization=0)
    cmd = build_command(status, change, profile)
    on = {f for f in funcs if f not in ("clean", "sterilize")}
    assert {f for f, v in _flags(cmd, funcs).items() if v == "0" and f in on} == cancelled & on


def test_humidity_change_cancels_like_temperature() -> None:
    status = _mode(_with(energy_conservation="1", health="1"), "dry_auto")
    cmd = build_command(status, {"humidity": 50})
    assert cmd["set_humidity"] == 50
    assert _flags(cmd)["save"] == "0" and _flags(cmd)["health"] == "1"


def test_standard_power_off_keeps_self_clean() -> None:
    status = _with(STANDARD.funcs, self_clean=1, health="1")
    cmd = build_command(status, {"power": False}, STANDARD)
    assert cmd["if_run"] == "0"
    assert cmd["self_clean"] == 1 and cmd["health"] == "0"
    cmd = build_command(status, {"power": False}, COLOR)
    assert cmd["self_clean"] == 0


def test_standard_mode_change_turns_off_k3_through_the_flags() -> None:
    """xkq-control sends PEOPLE's flag with the other flags."""
    status = _with(STANDARD.funcs, human_sense_type="2", independent_human_sense_effect="1")
    cmd = build_command(status, {"mode": "heat"}, STANDARD)
    assert cmd["independent_human_sense_effect"] == "0"
    assert "independent_human_sense_state" not in cmd
    assert "one_direction_human_sense" not in cmd
    # The colour page leaves the K3 sensing alone (not in its flags).
    cmd = build_command(
        _status(human_sense_type="2", independent_human_sense_effect="1"), {"mode": "heat"}
    )
    assert "independent_human_sense_effect" not in cmd


def test_standard_does_not_close_one_direction() -> None:
    status = _status(one_direction_human_sense="2")
    assert build_command(status, {"power": False}, STANDARD) == {"if_run": "0"}
    assert build_command(status, {"power": False}, COLOR) == {
        "if_run": "0",
        "one_direction_human_sense": "0",
    }


# ----------------------------------------------------------- human sensing


def test_one_direction_options() -> None:
    assert one_direction_options(_status()) == []
    assert one_direction_options(_status(human_sense_type="1")) == [
        "off",
        "blow",
        "avoid",
        "unmanned",
    ]
    # 3D sensor: no 无人节能.
    both = _status(human_sense_type="1", human_sense_type2="2")
    assert one_direction_options(both) == ["off", "blow", "avoid"]
    assert one_direction_options(_status(human_sense_type2="2")) == ["off", "blow", "avoid"]
    assert one_direction_options(_status(human_sense_type="1"), STANDARD) == []


def test_one_direction_on_cancels_clean_sterilize_no_wind() -> None:
    status = _with(human_sense_type="1", no_sense_of_wind="1", health="1")
    cmd = build_command(status, {"one_direction": "2"})
    assert cmd["one_direction_human_sense"] == "2"
    # PEOPLE_ONE entered the list: every offered flag is sent.
    flags = _flags(cmd)
    assert flags["no_wind"] == "0" and flags["clean"] == 0 and flags["sterilize"] == 0
    assert flags["health"] == "1"
    assert "one_direction_human_sense_effect" not in cmd


def test_one_direction_change_between_values_sends_only_the_value() -> None:
    status = _with(human_sense_type="1", one_direction_human_sense="1")
    assert build_command(status, {"one_direction": "3"}) == {"one_direction_human_sense": "3"}
    assert build_command(status, {"one_direction": "1"}) == {}
    cmd = build_command(status, {"one_direction": "0"})
    assert cmd["one_direction_human_sense"] == "0"
    assert _flags(cmd)["save"] == "0"  # the list changed: flags sent


def test_one_direction_refused() -> None:
    status = _status(human_sense_type="1", human_sense_type2="2")
    with pytest.raises(CommandRefused) as err:
        build_command(status, {"one_direction": "3"})
    assert err.value.key == "unsupported_option"
    with pytest.raises(CommandRefused) as err:
        build_command(_status(human_sense_type="1", if_run="0"), {"one_direction": "1"})
    assert err.value.key == "function_not_available"
    status = _status(human_sense_type="1", communication_exception="1", human_sense_type2="2")
    with pytest.raises(CommandRefused):
        build_command(status, {"one_direction": "1"})
    with pytest.raises(CommandRefused) as err:
        build_command(_status(), {"one_direction": "1"})
    assert err.value.key == "function_not_supported"


def test_one_direction_closes_far_wind() -> None:
    status = _status(
        human_sense_type="1",
        is_super_far_wind_avalable="1",
        ultra_far_air_supply="1",
        position_of_air_deflector1="0",
        section_of_air_deflector1="2",
    )
    cmd = build_command(status, {"one_direction": "1"})
    assert cmd["one_direction_human_sense"] == "1"
    # closeFarDirection: 上下风 selected again at its level (3).
    assert cmd["position_of_air_deflector1"] == "0"
    assert cmd["section_of_air_deflector1"] == "2"
    # Not when far wind is not selected.
    status["ultra_far_air_supply"] = "0"
    assert "position_of_air_deflector1" not in build_command(status, {"one_direction": "1"})


def test_3d_wind_blow_and_shelter() -> None:
    status = _with(human_sense_type2="2", forest_wind="1", health="1")
    status = {**status, "forest_wind": "0"}
    status["no_sense_of_wind"] = "1"
    cmd = build_command(status, {"one_direction": "1"})
    assert cmd["one_direction_human_sense"] == "1"
    flags = _flags(cmd, [*REGULAR, "smart_people"])
    assert flags["no_wind"] == "0" and flags["health"] == "1"
    shelter = {**status, "one_direction_human_sense": "1"}
    cmd = build_command(shelter, {"one_direction": "2"})
    assert cmd["one_direction_human_sense"] == "2"
    cmd = build_command(shelter, {"one_direction": "0"})
    assert cmd["one_direction_human_sense"] == "0"


def test_smart_sensing_clears_wind_blow_and_far() -> None:
    status = _with(
        human_sense_type2="2",
        one_direction_human_sense="2",
        energy_conservation="1",
        is_3D_wind_avalable="1",
        ultra_far_air_supply="1",
        position_of_air_deflector1="1",
    )
    cmd = build_command(status, {"funcs": {"smart_people": True}})
    assert cmd["human_sensing_control_3d"] == "1"
    assert cmd["energy_conservation"] == "0"
    assert cmd["one_direction_human_sense"] == "0"
    assert cmd["position_of_air_deflector1"] == "1"


def test_k3_settings_sent_together() -> None:
    status = _with(human_sense_type="2", no_sense_of_wind="1")
    assert k3_settings(status) == ("0", "1", "1")
    cmd = build_command(status, {"k3": {"effect": "1"}})
    assert cmd["independent_human_sense_effect"] == "1"
    assert cmd["independent_human_sense_state"] == "1"
    assert cmd["independent_human_sense_nobody_judge_time"] == "1"
    assert _flags(cmd)["no_wind"] == "0"
    # Only the setting: the three keys, no flags.
    on = {**status, "independent_human_sense_effect": "1", "no_sense_of_wind": "0"}
    assert build_command(on, {"k3": {"time": "4"}}) == {
        "independent_human_sense_effect": "1",
        "independent_human_sense_state": "1",
        "independent_human_sense_nobody_judge_time": "4",
    }
    assert build_command(on, {"k3": {"state": "1"}}) == {}
    with pytest.raises(CommandRefused):
        build_command(on, {"k3": {"state": "9"}})
    # Standard page: the same modal.
    standard = _with(STANDARD.funcs, human_sense_type="1")
    cmd = build_command(standard, {"k3": {"effect": "1"}}, STANDARD)
    assert cmd["independent_human_sense_effect"] == "1"


def test_k3_defaults_for_unreported_settings() -> None:
    status = {
        k: v
        for k, v in _status(human_sense_type="3").items()
        if not k.startswith("independent_human_sense_")
    }
    # The model's defaults are "0": no action, time "0" (30 min).
    assert k3_settings(status) == ("0", None, "0")
    assert build_command(status, {"k3": {"effect": "1"}}) == {
        "independent_human_sense_effect": "1",
        "independent_human_sense_state": "1",
        "independent_human_sense_nobody_judge_time": "0",
    }


def test_active_funcs_reads_like_get_funcs() -> None:
    status = _with(human_sense_type="1", one_direction_human_sense=2, self_clean=2, sleep=1)
    assert active_funcs(status, COLOR) == {"people_one", "sleep"}


# ----------------------------------------------------------------- humidity


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("dry_auto", ("mode", (35, 90))),
        ("dry_floor_heat", ("mode", (35, 90))),
        ("cool", None),
        ("dry", None),
        ("dry_comfort", None),
    ],
)
def test_humidity_modes(mode, expected) -> None:
    assert humidity_control(_mode(_status(), mode), mode) == expected
    assert humidity_control(_mode(_status(), mode), mode, STANDARD) == expected


def test_fresh_air_has_no_humidity_slider() -> None:
    status = _mode(_status(saveene_freshair="1"), "dry_auto")
    assert humidity_control(status, "dry_auto") is None
    # The standard page does not know fresh-air controllers.
    assert humidity_control(status, "dry_auto", STANDARD) == ("mode", (35, 90))


def test_humidification_target() -> None:
    status = _with(humidification="1")
    assert humidity_control(status, "cool") == ("humidify", (30, 60))
    assert humidity_control(_with(), "cool") is None
    # The modal: no function is cancelled.
    status = _with(humidification="1", energy_conservation="1")
    assert build_command(status, {"humidity": 40}) == {"set_humidity": 40}
    with pytest.raises(CommandRefused) as err:
        build_command(_with(), {"humidity": 40})
    assert err.value.key == "humidity_not_supported"
    # Humidification cannot be changed while off (HUMI enable rule).
    with pytest.raises(CommandRefused):
        build_command({**status, "if_run": "0"}, {"humidity": 45})


def test_humidification_on_with_humidity() -> None:
    status = _with(set_humidity=65)
    cmd = build_command(status, {"funcs": {"humidify": True}})
    assert cmd["humidification"] == "1"
    assert "set_humidity" not in cmd


@pytest.mark.parametrize(
    ("humidity", "mode", "expected"),
    [(80, "cool", 70), (80, "dry_comfort", None), (30, "heat", 35), (65, "heat", None)],
)
def test_mode_change_clamps_humidity(humidity, mode, expected) -> None:
    status = _mode(_status(set_humidity=humidity, is_dehumidification2_avalable="1"), "dry_auto")
    cmd = build_command(status, {"mode": mode})
    assert cmd.get("set_humidity") == expected


def test_unset_humidity_not_clamped() -> None:
    # The app's model default 0 would be clamped to 35 and sent; HA does not
    # invent a humidity for a controller that reports none.
    status = _mode(_status(set_humidity=0), "dry_auto")
    assert "set_humidity" not in build_command(status, {"mode": "cool"})
    assert current_humidity_setpoint(_status(set_humidity="55")) == 55
    assert current_humidity_setpoint(_status(set_humidity=None)) is None


# ------------------------------------------------------------ profile modes


def test_standard_modes_and_limits() -> None:
    status = _status()
    for mode in MODES:
        status[MODES[mode][1]] = "1"
    from custom_components.york_smart_home.control import supported_modes

    assert "fresh_air_eco" not in supported_modes(status, STANDARD)
    assert "heat_supply" not in supported_modes(status, STANDARD)
    assert len(supported_modes(status, STANDARD)) == 13
    assert len(supported_modes(status, COLOR)) == 15
    # getTempLimit: no CLEAR case on the standard page.
    assert temp_limits(status, "purify", STANDARD) == (1, 99)
    assert temp_limits(status, "purify", COLOR) == (16, 32)


def test_standard_keeps_auto_only_fan() -> None:
    status = _status(
        wire_controller_wind_low_fix="0",
        wire_controller_wind_mid_fix="0",
        wire_controller_wind_high_fix="0",
        wire_controller_wind_auto_fix="1",
    )
    assert supported_fans(status, COLOR) == []
    assert supported_fans(status, STANDARD) == ["auto"]


def test_command_state_is_the_command_for_plain_changes() -> None:
    cmd = {"if_run": "0", "one_direction_human_sense": "0"}
    assert command_state(_status(), cmd) == cmd
