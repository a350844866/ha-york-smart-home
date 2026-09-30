"""Constants for the York Smart Home (约克智慧家) integration."""

from typing import Final

DOMAIN: Final = "york_smart_home"

CONF_PHONE: Final = "phone"
CONF_CLIENT_ID: Final = "client_id"
CONF_SESSION: Final = "session"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_VERIFY_CODE: Final = "verify_code"
CONF_RECLAIM_SESSION: Final = "reclaim_session"
CONF_SESSION_YIELD_MINUTES: Final = "session_yield_minutes"

# Same as the app's home page: status every statRefTime (2s), sending the
# last report timestamp so a poll without news is a tiny empty answer.
DEFAULT_SCAN_INTERVAL: Final = 2  # seconds
MIN_SCAN_INTERVAL: Final = 2
MAX_SCAN_INTERVAL: Final = 3600

# A single failed poll (e.g. the cloud dropping the connection) keeps the
# last known state; entities become unavailable only after this many
# consecutive failures.
FAILURES_BEFORE_UNAVAILABLE: Final = 5  # ~10s at the 2s interval

# Intervals the app itself uses (onlineRefTime=120 in the login response).
ONLINE_INTERVAL: Final = 120
# Rediscover devices and re-read the full state this often.
DISCOVERY_INTERVAL: Final = 900

# HTTP 401 means the account logged in on another device (the app's
# LOGIN_ELSEWHERE; it never logs in again by itself). By default HA then
# leaves the session to that device for this long before logging in again.
DEFAULT_RECLAIM_SESSION: Final = False
DEFAULT_SESSION_YIELD_MINUTES: Final = 30
MIN_SESSION_YIELD_MINUTES: Final = 5
MAX_SESSION_YIELD_MINUTES: Final = 720
# When logging in again after a yield fails for another reason than the
# credentials (cloud outage), wait this long (doubling) before the next try.
RELOGIN_BACKOFF_MIN: Final = 60
RELOGIN_BACKOFF_MAX: Final = 900

# Business code "300" asks the app to update (APP_FORCE_UPDATE); stop
# polling for this long before trying again.
CLIENT_OUTDATED_PAUSE: Final = 3600

ISSUE_SESSION_TAKEN: Final = "session_taken"
ISSUE_CLIENT_OUTDATED: Final = "client_outdated"

# Right after a command the controller may still report its previous state
# (with a new timestamp). For this long, reports contradicting a field we just
# set are ignored for that field; a matching report ends it early.
COMMAND_GRACE: Final = 15

# Read a controller's full state once this long after a timer's end: the
# incremental reports are not known to carry the timer fields again once it
# has fired (the app re-reads everything whenever the control page opens).
TIMER_CHECK_DELAY: Final = 60

# The end the timer answer reports is computed by the cloud when it takes the
# request, the controller's own report a few seconds later (seen live: ~3 s).
# Ends this close are the same timer; the app's durations differ by 30 min.
TIMER_PIN_TOLERANCE_MS: Final = 5 * 60 * 1000

# Poll again this long after a command (ctrlRefTime=2) to pick up the
# controller's new report quickly.
REFRESH_DELAY_AFTER_CONTROL: Final = 2

API_BASE: Final = "https://1app.hicloud.hisensehitachi.com"
API_KEY: Final = "61PdDpLyYzpctYwwVpbHRb2"
APP_ID: Final = "com.hisensehitachi.iyes2"
APP_VERSION: Final = "1.8.2.260807104325_release"
APP_TAG: Final = "V1"

# ctrlType used by the app for single-device control ("SINGLE_CONTROL").
CTRL_TYPE_SINGLE: Final = "YO420"

# ---------------------------------------------------------------------------
# Wire-controller (线控器, "xkq") status/control keys, taken from the app's
# xkq-color-control service. Every mode/fan option is a boolean flag: to
# select one, the app sends {"<control_key>": "1"}. "<...>_fix" / statusKey
# fields in the controller constants (constantJson of the controller detail)
# tell whether the unit supports that option.
# ---------------------------------------------------------------------------

KEY_POWER: Final = "if_run"
KEY_SET_TEMP: Final = "set_temp"
KEY_SET_TEMP_HALF: Final = "set_temperature_add05"
KEY_INDOOR_TEMP: Final = "indoor_temperature"
KEY_INDOOR_HUMIDITY: Final = "indoor_humidity"
KEY_HALF_DEGREE_SUPPORTED: Final = "is_set_temperature05_avalable"
KEY_TIMESTAMP: Final = "timestamp"
# The app's home page sends a report's roundTime back as the next cursor.
KEY_ROUND_TIME: Final = "roundTime"
# "1" while the controller installs a firmware update (home page: full
# status query every poll, control refused).
KEY_OTA: Final = "otaFlg"
# One-direction human sensing: the app switches it off ("0") whenever the
# power or the mode changes (closeOneHumanFeeling).
KEY_ONE_DIRECTION_HUMAN_SENSE: Final = "one_direction_human_sense"
# Capability: "1" marks a fresh-air colour controller (xkqColorType WIND),
# for which the app hides the temperature and humidity setpoints.
KEY_FRESH_AIR_CONTROLLER: Final = "saveene_freshair"

# Filter reminder slots (filter_life_tips1..5) and the reset flag sent by the
# control page's filterScreenReset: ctrlJson {"screen_mark_reset": "1"}.
FILTER_TIPS_KEYS: Final = tuple(f"filter_life_tips{i}" for i in range(1, 6))
KEY_FILTER_RESET: Final = "screen_mark_reset"

# Colour controller settings page (xkq-color-set save()): the app always sends
# these six keys together, in this order. The lock/sound/mode keys are the
# strings "1"/"0"; the brightness values are numbers (ion-range 1..100).
KEY_KEY_LOCK: Final = "key_lock"
KEY_KEY_SOUND: Final = "key_sound_switch"
KEY_INDICATOR_MODE: Final = "indicator_light_mode"
KEY_BACKLIGHT_MODE: Final = "backlight_brightness_mode"
KEY_INDICATOR_BRIGHTNESS: Final = "indicator_light_brightness"
KEY_BACKLIGHT_BRIGHTNESS: Final = "backlight_brightness"
SETTINGS_KEYS: Final = (
    KEY_KEY_LOCK,
    KEY_KEY_SOUND,
    KEY_INDICATOR_MODE,
    KEY_BACKLIGHT_MODE,
    KEY_INDICATOR_BRIGHTNESS,
    KEY_BACKLIGHT_BRIGHTNESS,
)
# One "自动" toggle on the settings page drives both mode keys.
BRIGHTNESS_MODE_KEYS: Final = (KEY_INDICATOR_MODE, KEY_BACKLIGHT_MODE)
# The page's ion-range sliders: min 1, max 100, default step 1.
BRIGHTNESS_MIN: Final = 1
BRIGHTNESS_MAX: Final = 100

# Locks reported by the controller (xkq-color-control refreshSettingEnable).
LOCK_MODE: Final = "run_model_fixed"
LOCK_TEMP: Final = "set_temp_fixed"
LOCK_FAN: Final = "B9"

# Modes of the colour controller, in the app's order (xkq-color-control
# service allModeArr): mode id -> (control key, support key). The current mode
# is the first entry whose control key is "1". Some support keys are not
# *_fix flags; they are used exactly as the app does.
MODE_FRESH_AIR_ECO: Final = "fresh_air_eco"  # 节能新风 (SAVE_WIND)
MODE_COOL: Final = "cool"  # 制冷 (COLD)
MODE_HEAT: Final = "heat"  # 制热 (HOT)
MODE_FAN: Final = "fan_only"  # 送风 (WIND)
MODE_DRY: Final = "dry"  # 除湿 (WET)
MODE_AUTO: Final = "auto"  # 自动 (AUTO)
MODE_DRY_COMFORT: Final = "dry_comfort"  # 干爽除湿 (DRY_WET)
MODE_DRY_STRONG: Final = "dry_strong"  # 强力除湿 (STRONG_WET)
MODE_DRY_AUTO: Final = "dry_auto"  # 自动除湿 (AUTO_WET)
MODE_FLOOR_HEAT: Final = "floor_heat"  # 地暖 (FLOOR)
MODE_DRY_FLOOR_HEAT: Final = "dry_floor_heat"  # 除湿+地暖 (WET_FLOOR)
MODE_FAN_FLOOR_HEAT: Final = "fan_floor_heat"  # 送风+地暖 (WIND_FLOOR)
MODE_HEAT_FLOOR_HEAT: Final = "heat_floor_heat"  # 制热+地暖 (HOT_FLOOR)
MODE_PURIFY: Final = "purify"  # 净化 (CLEAR)
MODE_HEAT_SUPPLY: Final = "heat_supply"  # 供热 (HEATING)

MODES: Final = {
    MODE_FRESH_AIR_ECO: ("wire_controller_saveene_freshair", "saveene_freshair"),
    MODE_COOL: ("wire_controller_makecold", "wire_controller_makecold_fix"),
    MODE_HEAT: ("wire_controller_heating", "wire_controller_heating_fix"),
    MODE_FAN: ("wire_controller_makewind", "wire_controller_makewind_fix"),
    MODE_DRY: ("wire_controller_rmwet", "wire_controller_rmwet_fix"),
    MODE_AUTO: ("wire_controller_auto", "wire_controller_auto_fix"),
    MODE_DRY_COMFORT: ("wire_controller_dry_comfortable_rmwet", "is_dehumidification2_avalable"),
    MODE_DRY_STRONG: ("wire_controller_strong_rmwet", "is_dehumidification3_avalable"),
    MODE_DRY_AUTO: ("wire_controller_auto_rmwet", "wire_controller_auto_rmwet_fix"),
    MODE_FLOOR_HEAT: ("wire_controller_floor_heat", "is_floor_heating_avalable"),
    MODE_DRY_FLOOR_HEAT: ("wire_controller_rmwet_floor_heating", "controllerFloorRmwetFix"),
    MODE_FAN_FLOOR_HEAT: ("wire_controller_air_supply_floor_heating", "air_supply_floor_heating"),
    MODE_HEAT_FLOOR_HEAT: ("wire_controller_heat_floor_heating", "controllerFloorHeatFix"),
    MODE_PURIFY: ("wire_controller_purification", "is_purification_available"),
    MODE_HEAT_SUPPLY: ("wire_controller_heating_mode", "is_heating_mode_available"),
}

# The five basic modes; every mode belongs to the group of one of them
# (shown in HA as that HVAC mode, the specific mode as a preset).
BASIC_MODES: Final = (MODE_COOL, MODE_HEAT, MODE_FAN, MODE_DRY, MODE_AUTO)
MODE_GROUP: Final = {
    MODE_FRESH_AIR_ECO: MODE_FAN,
    MODE_COOL: MODE_COOL,
    MODE_HEAT: MODE_HEAT,
    MODE_FAN: MODE_FAN,
    MODE_DRY: MODE_DRY,
    MODE_AUTO: MODE_AUTO,
    MODE_DRY_COMFORT: MODE_DRY,
    MODE_DRY_STRONG: MODE_DRY,
    MODE_DRY_AUTO: MODE_DRY,
    MODE_FLOOR_HEAT: MODE_HEAT,
    MODE_DRY_FLOOR_HEAT: MODE_DRY,
    MODE_FAN_FLOOR_HEAT: MODE_FAN,
    MODE_HEAT_FLOOR_HEAT: MODE_HEAT,
    MODE_PURIFY: MODE_FAN,
    MODE_HEAT_SUPPLY: MODE_HEAT,
}

# Modes without a temperature setpoint on the app's control page
# (refreshTempEnable: AUTO_WET and WET_FLOOR set humidity instead; SAVE_WIND
# only exists on fresh-air controllers, which hide the setpoint entirely).
MODES_WITHOUT_TEMPERATURE: Final = frozenset(
    {MODE_FRESH_AIR_ECO, MODE_DRY_AUTO, MODE_DRY_FLOOR_HEAT}
)
# refreshWindDisplay: no fan speed in floor heating.
MODES_WITHOUT_FAN: Final = frozenset({MODE_FLOOR_HEAT})

# fan id -> (control key, support key); ordered from lowest to highest.
FAN_MODES: Final = {
    "silent": ("wire_controller_wind_light", "wire_controller_wind_silent"),
    "super_low": ("wire_controller_wind_superlow", "wire_controller_wind_superlow_fix"),
    "low": ("wire_controller_wind_low", "wire_controller_wind_low_fix"),
    "medium": ("wire_controller_wind_mid", "wire_controller_wind_mid_fix"),
    "high": ("wire_controller_wind_high", "wire_controller_wind_high_fix"),
    "super_high": ("wire_controller_wind_superhigh", "wire_controller_wind_superhign_fix"),
    "auto": ("wire_controller_wind_auto", "wire_controller_wind_auto_fix"),
}

# Temperature limits per mode (from xkq-color-control getTempLimit).
TEMP_LIMIT_COOL: Final = ("cold_down_value", "cold_up_value")
TEMP_LIMIT_HEAT: Final = ("heat_down_value", "heat_up_value")
TEMP_LIMIT_AUTO: Final = (16, 32)
# getTempLimit's default for modes it has no case for (SAVE_WIND).
TEMP_LIMIT_NONE: Final = (1, 99)
MODES_COOL_RANGE: Final = frozenset(
    {MODE_COOL, MODE_FAN, MODE_DRY, MODE_DRY_COMFORT, MODE_DRY_STRONG, MODE_DRY_AUTO}
)
MODES_HEAT_RANGE: Final = frozenset(
    {
        MODE_HEAT,
        MODE_FLOOR_HEAT,
        MODE_DRY_FLOOR_HEAT,
        MODE_FAN_FLOOR_HEAT,
        MODE_HEAT_FLOOR_HEAT,
        MODE_HEAT_SUPPLY,
        MODE_PURIFY,
    }
)

# ---------------------------------------------------------------------------
# Read-only running information (batch 3).
# ---------------------------------------------------------------------------

# Status banners of the colour controller page (xkq-color-control
# allowShowXX = "1" === controlModel.<key>; the key names keep the app's
# spellings). key -> enabled by default. Only for xkqType 9: the standard
# controller page (xkq-control, xkqType 8) has other keys (防冻结 is
# antifrozen_status there, and no defrost / auxiliary heat / micro pressure /
# 人感通讯异常 banners): STANDARD_STATUS_BANNERS below.
STATUS_BANNERS: Final = {
    "defrost_display": True,  # 除霜中 (allowShowCS)
    "pre_heat_display": True,  # 外机预热中 (allowShowYR)
    "mode_confict": True,  # 模式冲突 (allowShowCT)
    "central_controller": True,  # 集中控制 (allowShowJK)
    "auxiliay_heat_display": False,  # 辅热中 (allowShowFR)
    "mould_proof_status": False,  # 防霉运转 (allowShowFM)
    "water_pump_status": False,  # 水泵运行 (allowShowSB)
    "freeze_protection": False,  # 防冻结 (allowShowFD)
    "micro_positive_pressure": False,  # 微正压 (allowShowZY)
    "communication_exception": False,  # 人感通讯异常 (allowShowTX)
}
KEY_MODE_CONFLICT: Final = "mode_confict"
# allowShowTX also needs the 3D human sensor: "2" === human_sense_type2.
KEY_COMMUNICATION_EXCEPTION: Final = "communication_exception"
KEY_HUMAN_SENSE_TYPE2: Final = "human_sense_type2"
HUMAN_SENSE_3D: Final = "2"

# Keys of the home page's masking overlay (getXkqMaskingFlg).
KEY_SELF_CLEAN: Final = "self_clean"  # 1 自清洁中, 2 系统冷却中
KEY_STERILIZATION: Final = "hig_temperature_sterilization"  # 1 高温杀菌中
KEY_COMFORTABLE: Final = "comfortable"  # "1" 人感舒适中
KEY_ENERGY_SAVING: Final = "energy_saving"  # "1" 人感节能中
SELF_CLEAN_RUNNING: Final = 1
SELF_CLEAN_COOLING_DOWN: Final = 2

# Running states, in getXkqMaskingFlg's priority order (idle: no overlay).
RUNNING_SELF_CLEAN: Final = "self_clean"
RUNNING_STERILIZING: Final = "sterilizing"
RUNNING_COOLING_DOWN: Final = "cooling_down"
RUNNING_OTA: Final = "ota"
RUNNING_COMFORTABLE: Final = "comfortable"
RUNNING_ENERGY_SAVING: Final = "energy_saving"
RUNNING_IDLE: Final = "idle"
RUNNING_STATES: Final = (
    RUNNING_SELF_CLEAN,
    RUNNING_STERILIZING,
    RUNNING_COOLING_DOWN,
    RUNNING_OTA,
    RUNNING_COMFORTABLE,
    RUNNING_ENERGY_SAVING,
    RUNNING_IDLE,
)

# Indoor unit alarm slots: iu<n>_alarm_code / _system_no / _addr_no, n = 1..16.
ALARM_SLOTS: Final = range(1, 17)
# system_no / addr_no of an unused slot.
ALARM_UNUSED_SLOT: Final = 255
# fault_code sensor state when no indoor unit reports an alarm.
FAULT_CODE_NONE: Final = "none"

# ---------------------------------------------------------------------------
# Simple timer (简易定时, batch 4): the control page's "定时" picker
# (xkq-color-control showTimerModal -> xkq-control.service setTiming).
# ---------------------------------------------------------------------------

# Status fields, read from xkqStatusListGjy[0] into the page's control model
# (defaults 0; hasTimer = 1 === timerSimpleFlg).
KEY_TIMER_FLAG: Final = "timerSimpleFlg"  # 1 while a timer is set
KEY_TIMER_ONOFF: Final = "timerSimpleOnoff"  # 1: the timer turns the unit on, else off
KEY_TIMER_TIME: Final = "timerSimpleTime"  # when it fires, epoch milliseconds
TIMER_KEYS: Final = (KEY_TIMER_FLAG, KEY_TIMER_ONOFF, KEY_TIMER_TIME)
# ctrlJson of the simpleTimer request: timer_switch "1" = turn on at the end
# ("0" = turn off), simple_timer_time = duration in half hours.
KEY_TIMER_SWITCH: Final = "timer_switch"
KEY_SIMPLE_TIMER_TIME: Final = "simple_timer_time"
# The picker (timerColumnOptions): "关闭定时", 0.5 .. 10 h in half hours, then
# 11 .. 24 h in whole hours.
TIMER_HOURS: Final = (
    *(n / 2 for n in range(1, 21)),
    *(float(h) for h in range(11, 25)),
)
TIMER_OFF: Final = "off"

# ---------------------------------------------------------------------------
# Batch 5: capabilities of other controllers (extra functions, humidity, air
# deflectors, human sensing, fresh-air controllers, xkqType 8).
# ---------------------------------------------------------------------------

# Extra functions (allFuncArr), by the integration's id. Human sensing is not
# a simple flag (see the KEY_*HUMAN_SENSE* keys below).
FUNC_SAVE: Final = "save"  # 节能 energy_conservation
FUNC_SLEEP: Final = "sleep"  # 睡眠
FUNC_MUTE: Final = "mute"  # 静音 indoor_unit_silent
FUNC_HEALTH: Final = "health"  # 健康
FUNC_FOREST: Final = "forest"  # 森林风 forest_wind
FUNC_AUX_HEAT: Final = "aux_heat"  # 辅热 auxiliary_heat (the app's HOT)
FUNC_CLEAN: Final = "clean"  # 自清洁 self_clean (int)
FUNC_STRONG: Final = "strong"  # 强力 force_switch
FUNC_NO_WIND: Final = "no_wind"  # 无风感 no_sense_of_wind
FUNC_LEAVE: Final = "leave"  # 离家 away_home_energy_saving
FUNC_STERILIZE: Final = "sterilize"  # 高温杀菌 hig_temperature_sterilization (int)
FUNC_HUMIDIFY: Final = "humidify"  # 加湿 humidification
FUNC_SMART_PEOPLE: Final = "smart_people"  # 智慧人感 human_sensing_control_3d
# Human-sensing entries of allFuncArr, kept as ids so the page's function
# list can be mirrored (they are sent through their own keys).
FUNC_PEOPLE_ONE: Final = "people_one"  # 人感 (one direction), has_people_one
FUNC_PEOPLE: Final = "people"  # 人感 (independent / K3), independent_human_sense_effect
FUNC_WIND_BLOW: Final = "wind_blow"  # 风吹人 (3D sensor), one_direction_human_sense "1"
FUNC_WIND_SHELTER: Final = "wind_shelter"  # 风避人 (3D sensor), one_direction_human_sense "2"

# Names for the {function} placeholder of the refusal errors (Chinese, English):
# the entity names the functions have in the translations, so an error reads
# "现在不能切换辅热功能" rather than naming the internal id. The placeholder
# is filled in Home Assistant's language (see YorkCoordinator).
FUNC_HUMAN_SENSING: Final = "human_sensing"  # the human-sensing select as a whole
FUNC_LABELS: Final = {
    FUNC_SAVE: ("节能", "Eco"),
    FUNC_SLEEP: ("睡眠", "Sleep"),
    FUNC_MUTE: ("静音", "Mute"),
    FUNC_HEALTH: ("健康", "Health"),
    FUNC_FOREST: ("森林风", "Forest wind"),
    FUNC_AUX_HEAT: ("辅热", "Auxiliary heating"),
    FUNC_CLEAN: ("自清洁", "Self-clean"),
    FUNC_STRONG: ("强力", "Strong"),
    FUNC_NO_WIND: ("无风感", "Wind-free"),
    FUNC_LEAVE: ("离家", "Leave home"),
    FUNC_STERILIZE: ("高温杀菌", "High-temperature sterilization"),
    FUNC_HUMIDIFY: ("加湿", "Humidification"),
    FUNC_SMART_PEOPLE: ("智慧人感", "Smart human sensing"),
    FUNC_PEOPLE_ONE: ("人感", "Human sensing"),
    FUNC_PEOPLE: ("人感", "Human sensing"),
    FUNC_WIND_BLOW: ("风吹人", "Air to people"),
    FUNC_WIND_SHELTER: ("风避人", "Air avoids people"),
    FUNC_HUMAN_SENSING: ("人感", "Human sensing"),
}

KEY_SET_HUMIDITY: Final = "set_humidity"
KEY_HUMIDIFICATION: Final = "humidification"
KEY_HUMAN_SENSE_TYPE: Final = "human_sense_type"
# Independent (K3) human sensing: the page always sends the three together.
KEY_K3_EFFECT: Final = "independent_human_sense_effect"  # "0"/"1"
KEY_K3_STATE: Final = "independent_human_sense_state"  # nobody present: 1 run / 2 standby / 3 stop
KEY_K3_TIME: Final = "independent_human_sense_nobody_judge_time"  # "0".."4"
K3_KEYS: Final = (KEY_K3_EFFECT, KEY_K3_STATE, KEY_K3_TIME)
# FVqB modal: actionRange / timeRange (value -> option id).
K3_STATES: Final = {"1": "keep_running", "2": "standby", "3": "stop"}
K3_TIMES: Final = {"0": "30min", "1": "60min", "2": "90min", "3": "120min", "4": "180min"}
# rmSN modal (one direction): value -> option id; "3" not with a 3D sensor.
ONE_DIRECTION_OPTIONS: Final = {"0": "off", "1": "blow", "2": "avoid", "3": "unmanned"}

# Humidity setpoint (getHumiLimit): 35-90 in these modes, else 35-70. The
# humidification modal's slider is 30-60.
HUMIDITY_LIMIT: Final = (35, 70)
HUMIDITY_LIMIT_DRY: Final = (35, 90)
HUMIDIFICATION_LIMIT: Final = (30, 60)

# Air deflectors (导风板): per deflector N = 1..4.
KEY_H_DEFLECTORS: Final = "horizontal_air_deflector_num"
KEY_V_DEFLECTORS: Final = "vertical_air_deflector_num"
KEY_DEFLECTOR_ALONE: Final = "is_air_deflector_avalable"  # independent control allowed
KEY_DEFLECTOR_UNIFY: Final = "air_deflector_independent_or_together_ctrl"  # unified allowed
KEY_3D_WIND: Final = "is_3D_wind_avalable"
KEY_FAR_WIND: Final = "is_super_far_wind_avalable"
KEY_THREE_AIR_SUPPLY: Final = "three_air_supply"  # 3D出风 selected
KEY_ULTRA_FAR_AIR_SUPPLY: Final = "ultra_far_air_supply"  # 超远风 selected
DEFLECTOR_NUMBERS: Final = range(1, 5)
# Swing option ids: level 0 (position "1", the app's auto icon), fixed levels
# 1..7 (section level-1), and the two variants of 3D / far-wind units.
SWING_AUTO: Final = "auto"
SWING_THREE_D: Final = "three_d"
SWING_FAR: Final = "far"

# Fresh-air colour controllers (saveene_freshair "1", xkqColorType WIND):
# the page's air-quality tiles.
KEY_INDOOR_PM25: Final = "indoor_pm25"
KEY_INDOOR_CO2: Final = "indoor_co2"
KEY_INDOOR_TVOC: Final = "indoor_tvoc"

# Status banners of the standard controller page (xkq-control, xkqType 8):
# key -> enabled by default. 防冻结 is antifrozen_status there.
STANDARD_STATUS_BANNERS: Final = {
    "pre_heat_display": True,
    "mode_confict": True,
    "central_controller": True,
    "mould_proof_status": False,
    "water_pump_status": False,
    "antifrozen_status": False,
}
# Standard settings page (xkq-set): six levels (一档..六档) sent as 0..5.
STANDARD_BRIGHTNESS_LEVELS: Final = 6
