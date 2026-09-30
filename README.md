# 约克智慧家 (York Smart Home) — Home Assistant 集成

通过「约克智慧家」App 使用的云端接口（海信日立 hicloud 平台，`1app.hicloud.hisensehitachi.com`），
把约克中央空调的 **Wi-Fi 线控器** 接入 Home Assistant。

协议来自对 App（`com.hisensehitachi.iyes2` v1.8.x）抓包，以及对其 Ionic 前端代码
（`xkq-color-control` 彩屏线控器页面、`xkq-control` 普通线控器页面）的分析。

## 功能

每个线控器会生成一个设备，包含：

| 实体 | 说明 |
| --- | --- |
| `climate` | 开关机、模式（按设备能力显示，彩屏线控器的 15 种模式都支持，见下文）、设定温度（0.5°C 步进）、风速（微风/超低/低/中/高/超高/自动，按设备能力显示）。有导风板的设备还有风向（swing），可设湿度的模式和加湿开启时还有目标湿度（见下文「其他设备的功能」）。属性 `mode_locked` / `temperature_locked` / `fan_locked` 显示线控器的锁定状态 |
| `sensor` 室内温度 | 线控器测得的室温；数据缺失或异常时显示「未知」 |
| `binary_sensor` 故障 | 任一内机报警时为「开」，属性 `alarms` 中是故障代码（十六进制，与 App 显示一致）、系统号、内机号和故障说明（见下文）；没有读到故障字段时显示「未知」，不会误报「正常」。属性 `raw_alarm` 与 App 的红色报警图标一致（见下文） |
| `sensor` 故障代码（诊断） | 第一个报警内机的故障代码，没有故障时为 `none`，没有读到故障字段时为「未知」；属性中有该故障的系统号、内机号和说明 |
| `sensor` 运行状态 | App 首页线控器卡片上的遮罩提示：自清洁中 / 高温杀菌中 / 系统冷却中 / OTA升级中 / 人感舒适中 / 人感节能中，没有遮罩时为「正常」 |
| `binary_sensor` 状态提示（诊断） | App 控制页顶部的提示条：除霜中、外机预热中、模式冲突、集中控制（默认启用），辅热中、防霉运转、水泵运行、防冻结、微正压、人感通讯异常（默认禁用，可在实体设置里启用）。线控器上报了对应字段才创建 |
| `binary_sensor` 滤网清洗提醒 | 线控器提示清洗滤网时为「开」；没有数据时显示「未知」 |
| `button` 滤网复位（配置） | 清洗滤网后按下，和 App 控制页的「滤网复位」相同；线控器上报了 `filter_life_tips*` 才创建 |
| `switch` 按键锁定 / 按键音 / 自动亮度（配置） | 对应 App 彩屏线控器设置页的「按键操作锁定」「触控音」「亮度调节 → 自动」；只在彩屏线控器（`xkqType=9`）且上报了对应字段时创建 |
| `number` 背光亮度 / 运行指示灯亮度（配置） | 1~100，步进 1（与 App 设置页的滑块相同）；自动亮度开启时不能调节。线控器同时上报了两个亮度模式字段才创建（否则自动亮度永远算开启，也无法关闭，滑块永远不能用） |
| `select` 定时 | App 控制页的「定时」（简易定时）：关闭定时，0.5~10 小时（每 0.5 小时一档），11~24 小时（每小时一档）。选时长就设置新的定时，选「关闭定时」取消。属性 `action` 是正在运行的定时到点后的动作（`on` 开机 / `off` 关机），`next_action` 是现在设置新定时的动作。线控器上报了定时字段才创建 |
| `sensor` 定时结束 | 定时到点的时间（时间戳）；没有定时或上报的时间不合理时为「未知」。属性 `action`：到点后开机（`on`）还是关机（`off`） |
| `switch` 附加功能 | 节能、睡眠、静音、健康、森林风、辅热、自清洁、强力、无风感、离家、高温杀菌、加湿、智慧人感：设备提供哪个功能就创建哪个（你的设备一个都没有）。规则见下文「其他设备的功能」 |
| `switch` 人感 / `select` 人感：无人时、无人判定时间 | 独立人感（K3，`human_sense_type` 为 2/3，普通线控器为非 0）的开关和两个设定 |
| `select` 人感 | 单向人感（`human_sense_type="1"`）：关闭 / 风吹人 / 风避人 / 无人节能；带 3D 人感的设备是风吹人 / 风避人 |
| `select` 导风板 2~4 | 第 1 块导风板在 `climate` 的风向里，其余每块一个选择器（设备允许单独控制时） |
| `sensor` 室内湿度 / PM2.5 / CO₂ / TVOC | 只有新风型彩屏线控器才创建（见下文） |

- **模式**：与 App 的 `allModeArr` 一致，共 15 种，按设备能力位显示。HA 的 HVAC 模式只有 5 种，所以每种模式归到一组，具体模式作为「预设」（preset）显示：

  | HVAC 模式 | 包含的模式（预设名） |
  | --- | --- |
  | 制冷 | 制冷 |
  | 制热 | 制热、供热、地暖、制热+地暖 |
  | 送风 | 送风、节能新风、送风+地暖、净化 |
  | 除湿 | 除湿、干爽除湿、强力除湿、自动除湿、除湿+地暖 |
  | 自动 | 自动 |

  - 只有制冷/制热/送风/除湿/自动这几种基本模式的设备（例如常见的四模式机）不显示预设，界面和以前一样。
  - 当前模式按 App 的方式判断：按上表 App 的顺序，第一个控制位为 `"1"` 的模式。
  - 选 HVAC 模式时：已经在该组的某个模式里就保持不变；否则发送该组的基本模式，设备没有基本模式时发送该组里第一个支持的模式。选预设就发送该模式本身（关机时同时开机）。
- 温度范围按模式取自设备（App 的 getTempLimit）：制冷/送风/除湿/干爽除湿/强力除湿/自动除湿用 `cold_down_value`~`cold_up_value`；制热/供热/各地暖模式/净化用 `heat_down_value`~`heat_up_value`；自动模式 16~32°C。
  - 送风模式和 App 一样可以设温度（制冷范围）。
  - 自动除湿、除湿+地暖在 App 里设湿度而不是温度，节能新风没有温度范围，这几种模式下不显示设定温度；新风型彩屏线控器（能力位 `saveene_freshair="1"`）在所有模式下都不显示设定温度。
  - 地暖模式下不显示风速（App 也隐藏）。
- 温度步进固定 0.5°C（App 的线控器页面写死 0.5），只有设备明确报 `is_set_temperature05_avalable="0"` 时才按整度。
- 控制方式与 App 的线控器详情页一致（但不做 App 的 1.5 秒防抖，每次操作立即发送）：
  - 只发送与当前状态不同的字段，没有变化就不发请求；
  - 切换模式时，设定温度会被调整到新模式的温度范围内；
  - 调温、调风速、切换模式、关机时，会像 App 一样关掉相应的附加功能（节能、静音等）；切换模式只保留「健康」，切到地暖时连「健康」也关掉；
  - 开关机或切换模式时，如果单向人感（`one_direction_human_sense`）开着，同一条指令里会把它设为 `"0"`（App 的 closeOneHumanFeeling）；
  - 云端返回 `code 200` 就算成功（App 详情页也不查询执行回执）。界面先显示新状态，
    之后以线控器实际上报的状态为准。
- 线控器在面板上锁定了模式、温度或风量（`run_model_fixed` / `set_temp_fixed` / `B9`）时，相应操作会被直接拒绝，和 App 的行为一致。开关机不受影响。
- 能力位、锁定位和温度范围和 App 一样取自线控器详情接口（`GET /api/appdevice/homes/{homeId}/xkqs/{xkqCode}` 的 `constantJson`）。App 每次打开控制页读一次，插件在启动时和每 15 分钟重新发现设备时读一次，所以面板上的锁定最迟 15 分钟后在 HA 生效。
  详情接口失败或没有返回 `constantJson` 时退回设备列表里的 `constantJson`。这些键以详情里的值为准，状态上报里即使有同名字段也不覆盖（和 App 一样，值为空（null 或空字符串）的常量不覆盖）；其余字段取实时状态。控制指令同样按合并后的数据计算（温度范围、可用功能）。
- 状态轮询方式与 App 首页一致：带上一次上报的 `roundTime`（没有时用 `timestamp`）做增量查询，没有新上报时云端返回空列表；判断上报是否过期也用同一个字段。
  默认和 App 一样每 2 秒查询一次。每 15 分钟重新读取一次完整状态。
  面板上的改动一般在一个轮询间隔内同步到 HA。
- 线控器固件升级中（上报 `otaFlg="1"`）时，和 App 首页一样每次都查询完整状态，并拒绝控制（提示「线控器正在升级中，无法控制」）。
- **状态提示**：与 App 控制页的 `allowShowXX` 一致，每个提示对应一个上报字段，值为 `"1"` 时为「开」（也接受整数 1），缺失或不是数字时为「未知」。字段名保留 App 的拼写：
  `defrost_display` 除霜中、`pre_heat_display` 外机预热中、`mode_confict` 模式冲突（问题类）、`central_controller` 集中控制、`auxiliay_heat_display` 辅热中、`mould_proof_status` 防霉运转、`water_pump_status` 水泵运行、`freeze_protection` 防冻结、`micro_positive_pressure` 微正压、`communication_exception` 人感通讯异常。
  人感通讯异常只在设备带 3D 人感（能力位 `human_sense_type2="2"`）时有意义（App 也只在这时显示），否则不创建。其他型号的线控器用的是别的字段名，所以只按上报的字段逐个创建。
  App 页面上「除霜中」写作「除霜显示」，「外机预热中」写作「预热中」，这里用更直白的名称。这组字段是彩屏线控器控制页（`xkqType=9`）的；普通线控器页面（`xkqType=8`）只有 6 个：外机预热中、模式冲突、集中控制（默认启用），防霉运转、水泵运行、防冻结（`antifrozen_status`，默认禁用）。
- **运行状态**：按 App 首页 getXkqMaskingFlg 的顺序判断：`self_clean==1` 自清洁中 → `hig_temperature_sterilization==1` 高温杀菌中 → `self_clean==2` 系统冷却中 → `otaFlg==1` OTA升级中 → `comfortable=="1"` 人感舒适中 → `energy_saving=="1"` 人感节能中 → 正常。字符串和数字都接受（App 对高温杀菌严格比较数字 1，线控器上报的也是数字）。
  系统冷却中（`self_clean==2`）App 控制页会弹出「系统冷却中」对话框并退出控制页，首页卡片也被遮住，所以这时 HA 拒绝所有控制和设置指令（提示「系统冷却中（自清洁结束后），暂时无法控制。」），发送设置前重读状态时发现冷却中也不发送。优先级与 App 相同：同时上报高温杀菌时按高温杀菌处理，不拒绝。HA 把会话让给 App 期间不轮询，缓存的状态可能已经过时，所以这时先收回会话、重读状态再判断。只有线控器根本不具备的功能、风向或选项（只取决于能力位，不取决于开关机和模式）在收回会话之前就直接报错，不会为此把手机 App 挤下线。
  自清洁、高温杀菌进行中 HA 不拒绝，任何指令都会发到线控器；App 这时会弹出对话框，只能返回或停止自清洁/高温杀菌。
- **故障说明**：内置 App 的故障代码表（`assets/data/trouble-code.json`，88 条，生成为 `trouble_codes.py`），按代码查找时不区分大小写（表里混用了 `0b`、`bF` 这样的写法）。33、03、05 这三个代码优先用 App 控制页自己的提示文字（getAlarmInfo）。`alarms` 里每条故障带 `description`，表中有第二、第三原因时还带 `reason_two` / `reason_three`；表里没有的代码没有说明。
  `raw_alarm`：App 的红色报警图标（getIsAlarm）只看任一 `iu*_alarm_code` 在 JavaScript 里为真且不是字符串 `"0"`，不排除系统号/内机号为 255 的空槽位；故障列表（和 `binary_sensor` 故障的状态）会跳过这些空槽位，所以两者可能不一致。和 App 一样，只有系统号或内机号为 255 才跳过；没有上报系统号/内机号的槽位仍然列出，只是不带 `system_no` / `indoor_unit_no`。
- 偶尔一次轮询失败（例如云端断开连接）会继续显示上一次的状态，连续 5 次（约 10 秒）失败才显示为「不可用」。登录失效会立即提示重新登录。
- 线控器离线时实体显示为「不可用」，也不会发送指令。在线状态按 App 的周期每 120 秒查询一次。
- 每 15 分钟重新发现设备，新绑定的线控器会自动出现，无需重载集成。只接入本协议支持的约克 Wi-Fi 线控器（`brand=3`、`type=C`、`xkqType=9` 彩屏线控器或 `xkqType=8` 普通线控器），其他型号会跳过，数量记在诊断信息里。
- 状态与之前的数据合并，时间戳更旧的上报会被丢弃；能力位、模式位同时兼容字符串 `"1"`、整数 `1` 和布尔值。
- 云端返回 HTTP 429 时，按 `Retry-After` 暂停轮询。
- 云端返回业务码 `300`（App 的强制升级提示）时，暂停轮询 1 小时，并在「设置 → 修复」里提示（包含云端给出的新版本号和下载地址），1 小时后自动重试，恢复后提示自动消失。HA 启动时就遇到 `300` 也一样：集成照常加载（暂时没有设备），不会按 HA 的启动重试节奏反复访问云端。控制指令遇到 `300` 同样开始暂停。
- 账号在其他设备登录（HTTP 401）的处理见下文「与手机 App 共用账号」。其他登录失效（例如 HTTP 403）时自动续期（密码登录用密码重新登录；短信登录用 refreshToken 续期，与 App 的自动登录相同），新会话会保存下来，重启 HA 不需要重新登录。密码登录的响应不带 refreshToken 时，保留之前保存的 refreshToken。
- **滤网复位**：发送 `{"screen_mark_reset":"1"}`（App 控制页 filterScreenReset，同样走 `multiIuOuCtrl` / `YO420`，不是 `YO470`）。和 App 一样，这条指令不经过模式/温度的差异计算，也不会关掉任何附加功能。
  成功后先在本地清掉滤网提醒（提示清洗的 `filter_life_tips*` 置 0，15 秒内不被上报覆盖，线控器可能还没处理完复位）；15 秒后的第一次轮询读取一次完整状态（cursor 0），以线控器自己的 `filter_life_tips*` 为准：复位没有生效时提醒会重新出现。App 也是先隐藏滤网提示，下一次状态仍提示时再显示。App 只在滤网提醒出现时提供复位入口，HA 的按钮随时可以按。普通线控器（`xkqType=8`）控制页的滤网复位是同一条指令。
- **线控器设置**（按键锁定、按键音、自动亮度、背光亮度、运行指示灯亮度）与 App 设置页的 `save()` 一致：每次都发送完整的 6 个字段
  `key_lock`、`key_sound_switch`、`indicator_light_mode`、`backlight_brightness_mode`、`indicator_light_brightness`、`backlight_brightness`，
  由线控器的当前状态补齐，只替换改动的那一项。和 App 设置页打开时用 timestamp 0 读取完整状态一样，HA 每次发送前先读一次完整状态（cursor 0），在墙上线控器刚改过的设置不会被旧值覆盖（从其他设备收回登录时本来就会重读，不再多读一次）；读取失败或线控器正在升级时不发送。要设置的值和当前值相同时什么都不发送（自动化反复设置同一个值不会访问控制接口）。这些字段只取实时状态，不会被 constantJson 里的同名字段覆盖（App 设置页只读 `xkqStatusListGjy[0]`）。前四个是字符串 `"1"`/`"0"`，亮度是数字（没改的亮度按线控器上报的原值发送；线控器没有上报的亮度字段不发送，和 App 里 undefined 被 JSON 丢掉一样）。
  - 自动亮度：两个 mode 字段只要有一个不是 `"0"` 就算自动（App 的判断方式）；打开/关闭时两个 mode 字段同时写 `"1"`/`"0"`。
  - 自动亮度开启时 App 禁用亮度滑块，HA 调亮度会直接报错（提示先关闭自动亮度），不会偷偷关掉自动亮度。
  - 和空调控制一样：界面先显示新值，15 秒内不被旧上报覆盖；线控器离线或升级中时拒绝发送；同一线控器的指令依次发送。
  - 设置页上的其他项目只有名称/背景图（`MODIFY_XKQ_NAME`，不是线控器指令）、OTA 升级和电子说明书，没有实现。
    上报里还有 `set_backlight_duration`（背光时长）、`temp_humidity_report_interval` / `status_reporting_interval`（上报间隔）、`indoor_temperature_display`、`shutdown_display_content`、`atmosphere_lamp_switch` 等字段，但 App 没有任何页面写这些字段，写入格式无从确认，所以没有做成实体。
  - 亮度按四舍五入取整数发送（2.5 → 3）。
  - 普通线控器（`xkqType=8`，设置页 `xkq-set`）的编码不同：按页面顺序只发 4 个字段 `key_lock`、`backlight_brightness`、`backlight_brightness_mode`、`key_sound_switch`；亮度是「一档~六档」的序号 0~5（自动时发 0），没有指示灯字段。HA 里是按键锁定、按键音、自动亮度三个开关和一个「背光亮度」数值（1~6 对应一档~六档，自动时为「未知」）。自动只看 `backlight_brightness_mode`（不是 `"0"` 就算自动）。App 的档位和「自动」在同一个选择器里，所以在 HA 里选档位会同时关掉自动；关掉自动时沿用线控器上报的档位（没有有效档位时为一档）。
- **定时（简易定时）**：与 App 彩屏线控器控制页的定时选择器（showTimerModal）一致，普通线控器页面（`xkqType=8`）是同一套代码。
  - 设置：`POST /api/appdevice/homes/{homeId}/xkqs/{xkqCode}/simpleTimer`，请求体 `{"timerSimpleMinute": 小时×60, "ctrlJson": {"timer_switch": "0"|"1", "simple_timer_time": 小时×2}}`。`timerSimpleMinute` 和 `simple_timer_time`（单位半小时）是数字，`timer_switch` 是字符串。例如 0.5 小时发 `30` 和 `1`。
  - 动作总是当前电源状态的反向（App 发 `timer_switch: isOn ? "0" : "1"`）：开着就是定时关机（`"0"`），关着就是定时开机（`"1"`），不能单独选择。所以 HA 设置前先读取一次完整状态（cursor 0），按线控器当前的开关机状态决定动作；属性 `next_action` 显示现在设置会是哪种。
  - 取消：同一个 URL，`{"timerSimpleMinute":0, "ctrlJson":{"simple_timer_time":0}}`。App 从不调用 `DELETE .../simpleTimer/{timerSimpleTime}`（XKQ_DELETE_TIMING），HA 也不用。和 App 一样，没有定时时选「关闭定时」什么都不发送。
  - 状态来自普通的状态上报（`xkqStatusListGjy`，App 控制页也从这里读）：`timerSimpleFlg`（1 表示有定时，App 严格比较数字 1，HA 也接受 `"1"`；null/缺失表示没有定时）、`timerSimpleOnoff`（1 表示到点开机，否则关机）、`timerSimpleTime`（到点时间，epoch 毫秒）。这些字段只取实时状态，不会被 constantJson 覆盖。
  - 到点时间的单位：App 用 `new Date(timerSimpleTime)` 显示时间，剩余时间按 `(timerSimpleTime - 现在) / 1800000` 算（getTimeRemainin；彩屏页用接口返回的 `systemTime` 作「现在」，普通页用手机时钟），所以是和时钟同一基准的毫秒时间戳。传感器直接显示这个时间；不是数字、为 0 或在未来超过 25 小时（最长定时 24 小时）时视为不合理，显示「未知」。
  - 到点之后：到点时间过去超过 10 分钟、云端却还报着定时时，视为已经执行，「定时结束」显示「未知」，选择器也显示「未知」、不带 `action` 属性（App 这时会显示「10分钟内」，但在 HA 里把过去的时间当作下次定时会误导）。增量上报是否会在定时执行后再带上定时字段不确定，所以 HA 在每个定时的到点时间 60 秒后读取一次完整状态（每个到点时间只读一次），不用等 15 分钟一次的全量刷新。
  - 状态里只有到点时间，没有时长。选择器只对在 HA 设置的定时显示所选时长，并且只在云端报的还是这个定时时（到点时间和设置返回的一样，或相差不超过 5 分钟——实测设置返回的到点时间由云端在收到请求时计算，线控器执行后上报的晚约 3 秒，这时改用上报的时间；返回里没有时间时，以宽限期后云端报的第一个到点时间为准，之后到点时间变了就说明在别处改过）；其他情况显示「未知」（到点时间看「定时结束」传感器）。上报的 `simple_timer_time` 不用来推断时长：它是请求里的字段，App 从不读取上报的值，没有定时时实机上报 `255`，定时进行中可能是剩余的半小时数而不是设置的时长，确认之前不使用（诊断信息里能看到它的值）。
  - 云端返回 `code 200` 就算成功（App 不检查返回码，直接提示成功），其他返回码报「控制失败」。成功后界面立刻显示新定时（`timerSimpleTime` 取返回的 `data.timerSimpleTime`；返回里没有时就显示「未知」，不沿用被替换的旧定时的时间），15 秒内不被旧上报覆盖；15 秒后第一次轮询读取完整状态，以云端的定时字段为准。
  - 和其他指令一样：线控器离线、升级中、系统冷却中时拒绝；会话让给 App 期间会先收回会话、重读状态；同一线控器的指令依次发送。
  - 有定时时开关机：定时是按设置时的开关机状态定的，开关机后云端会保留还是取消定时由云端决定，所以这时 15 秒后同样读取一次完整状态，让两个实体跟上云端。
  - App 选中「睡眠」时显示的「8小时内关机」只是 App 本地的提示，不是定时，这里不当作定时。
- **其他设备的功能**（你的设备没有这些能力，按能力位自动出现；全部来自 App 代码，还没在真机上验证）：
  - **两种线控器页面**：彩屏线控器（`xkqType=9`）按 App 的 `xkq-color-control` 页面处理，普通线控器（`xkqType=8`）按 `xkq-control` 页面处理。两者的指令字段相同，但规则有差别（代码里的 `profile.py`）：普通线控器只有 13 种模式（没有节能新风、供热），净化模式没有温度范围（App 的 getTempLimit 没有这一项，不限制）；附加功能只要能力位不是 `"0"` 就提供（彩屏要求 `"1"`）；调温/调湿只取消节能，调风速取消节能、静音、森林风、强力、无风感；关机时正在进行的自清洁保留；人感功能按 `human_sense_type` 判断，发送附加功能时连同人感开关（`independent_human_sense_effect`）一起发；高温杀菌关闭时发字符串 `"0"`（彩屏发数字 0）；互斥表更短（例如离家不关掉其他功能）；开关机、切模式、调风向不会关掉单向人感；滤网提醒只看 `filter_life_tips1 == 2`；状态提示和设置页见上文。
  - **附加功能开关**（App 控制页的「功能」）：与 App 的 funcsChanged 一致。打开一个功能时，会关掉 App 同时关掉的功能（完整互斥表见 `profile.py`，与 App 代码逐项对照测试），并且和 App 一样一次发送所有可用功能的开关位（`self_clean`、`hig_temperature_sterilization` 是数字 1/0，其余是字符串 `"1"`/`"0"`）。副作用也一样：
    - 睡眠：设备有 3~4 档风速时同时把风速设为低风，6~7 档时设为微风（只要睡眠开着，任何功能变动都会再设一次，和 App 相同）；
    - 离家：设定温度改为 `max(heat_down_value, 19)`；
    - 自清洁、高温杀菌：只能在关机时开启，同时发 `if_run:"1"`（和其他功能位一起）；关闭就是 App 遮罩上的「终止」按钮：`{"self_clean":0,"if_run":"0"}`（高温杀菌同理），**会同时关机**；
    - 加湿：App 打开的是一个带湿度滑块（30~60%）的弹窗，这里是「加湿」开关加上 `climate` 的目标湿度（加湿开启时可设 30~60%，只发 `set_humidity`，不取消其他功能），开关加湿本身不关掉任何功能；
    - 智慧人感：App 先弹出确认框，确认后关掉节能、静音、睡眠、辅热、森林风、无风感、离家、强力、风吹人、风避人、自清洁、高温杀菌，并把「超远风」切回上下风。
  - **何时可以切换**：App 按开关机状态、模式和锁定位禁用部分功能（refreshFuncEnable，例如辅热只在制热、森林风只在制冷、离家只在地暖、自清洁和高温杀菌只在关机时、人感通讯异常时人感类功能不可用）。HA 里这些开关一直可用（能看到状态），但在 App 不允许的状态下切换会直接报错（例如「现在不能切换辅热功能……」，功能名按 HA 的语言显示中文或英文），什么都不发送；属性 `changeable` 显示当前能否切换。已经是要求的状态时不报错也不发送。
  - **目标湿度**：自动除湿、除湿+地暖模式下 App 显示湿度滑块而不是温度（新风型除外），`climate` 这时支持目标湿度，范围按 App 的 getHumiLimit 为 35~90%（其他模式是 35~70%）；设湿度和调温一样会取消节能和智慧人感。切换模式时，和 App 一样把设定湿度调整到新模式的范围内并发送（例如 80% 切到制冷变成 70%）；线控器没有上报有效湿度（0 或缺失）时不发送（App 会把默认值 0 调成 35 发出去，HA 不编造湿度）。
  - **风向（导风板）**：数量取自能力位 `horizontal_air_deflector_num`（上下，先编号）和 `vertical_air_deflector_num`（左右）。每块导风板有「自动」（`position_of_air_deflectorN:"1"`，App 显示自动图标，推测是摆风）和固定档位 k（`position…:"0"` 加 `section_of_air_deflectorN: k-1`）。最大档位按模式：单独控制时上下导风板在制冷/除湿/制热/送风/自动/制热+地暖/自动除湿为 7 档，其他模式 5 档；统一控制时制热/送风/制热+地暖/送风+地暖/供热为 7 档，其他 5 档；左右导风板总是 7 档。线控器报的档位超过最大值时按最大值显示（App 弹窗也这样处理）。
    - `climate` 的风向对应第 1 块导风板，其余每块一个「导风板 N」选择器。和 App 的 controlDirection 一样，每次都发送**所有**导风板（其他导风板用当前档位，超过当前模式最大值的按最大值），每块都带 `air_deflectorN_independent_or_together_ctrl:"0"`；读不到档位的导风板不发送（App 会发 `"NaN"`）。
    - 只允许统一控制时（`is_air_deflector_avalable` 不是 `"1"` 而 `air_deflector_independent_or_together_ctrl="1"`；彩屏页面上只有 1 块上下导风板又没有 3D/超远风时也强制统一控制），没有选择器，`climate` 的风向把所有导风板设成同一档，标志位为 `"1"`。
    - 带 3D 出风或超远风的设备（`is_3D_wind_avalable` / `is_super_far_wind_avalable`）：风向里多出「3D出风」「超远风」两个选项，第 2 块是「左右风」选择器（实体名就叫「左右风」）。和 App 一样一次只发选中的那一项（上下风/左右风的档位，或 `three_air_supply:"1"` / `ultra_far_air_supply:"1"`），不带标志位。App 从不发 `"0"` 关掉 3D/超远风，HA 也不发，只在本地显示为已切换，15 秒后读取一次完整状态，以线控器上报为准。
    - 只有开机且不在地暖模式时才能调风向（地暖模式下 `climate` 没有风向，选择器报错），和 App 一样调风向会取消无风感、森林风、智慧人感、风吹人、风避人，并关掉单向人感（普通线控器只取消无风感、森林风）。App 分两次发送（风向立即发，功能变动 1.5 秒后发），HA 合并在一条指令里。
  - **人感**：
    - 单向人感（`human_sense_type="1"`，App 的人感弹窗）：`one_direction_human_sense` 取值 `"0"` 关闭、`"1"` 风吹人、`"2"` 风避人、`"3"` 无人节能（带 3D 人感时没有无人节能）。打开时关掉自清洁、高温杀菌、无风感（因此和 App 一样连同所有功能位一起发送），并把超远风切回上下风。
    - 3D 人感（`human_sense_type2="2"` 且未锁温度/风量）：风吹人、风避人是两个功能，同样用这个选择器（没有无人节能），互斥规则按 App 的风吹人/风避人。
    - 独立人感（K3）：开关、「无人时」（继续运行/待机/停机）、「无人判定时间」（30/60/90/120/180 分钟）。和 App 一样三个字段 `independent_human_sense_effect` / `_state` / `_nobody_judge_time` 总是一起发送；打开时关掉自清洁、高温杀菌、无风感。两个设定是配置类实体（显示在设备页的「配置」里）。
    - App 在任何功能变动时都会把「没选中」的人感重置为 0，即使设备没有这个功能；HA 只对提供该功能的设备这样做，不会给没有人感的设备发人感字段。
  - **新风型彩屏线控器**（能力位 `saveene_freshair="1"`，App 的 xkqColorType WIND）：没有设定温度和湿度；`sensor` 室内湿度（%）、室内 PM2.5（μg/m³）、室内 CO₂（ppm）、室内 TVOC（等级，0 优 / 1 良 / ≥2 差，无单位）在线控器上报了该字段时创建，数值不合理时（湿度 1~100%、PM2.5 0~1000、CO₂ 1~10000、TVOC 0~10 以外，例如传感器预热时 CO₂ 报 0）显示「未知」，数值恢复正常后自动显示（传感器不会因为 HA 启动时数值不合理而缺失），和 App 一样关机时显示「未知」（App 显示「--」）；`climate` 的当前湿度也取室内湿度。滤网提醒的属性 `filters` 列出 5 个滤网（初效、加湿、活性炭、新风侧 PM2.5、回风侧 PM2.5）各自是 `ok`、`clean`（需清洁）还是 `replace`（需更换），与 App 的滤网弹窗一致。
- 所有报错（离线、升级中、系统冷却中、锁定、功能当前不能切换、控制失败等）都有中英文翻译。
- 支持下载诊断信息。诊断用白名单导出，只包含协议类型、固件版本、能力位和空调状态（包括状态提示、运行状态、故障槽位和定时的字段），不含手机号、密码、token、设备编号、名称和位置。

## 安装

**手动安装**：把 `custom_components/york_smart_home` 整个目录复制到 HA 配置目录下的
`custom_components/` 中，然后重启 Home Assistant。

**HACS**：在 HACS → 右上角菜单 → 自定义存储库中添加 `https://github.com/mufeng05/ha-york-smart-home`，类别选 Integration，然后在 HACS 里下载「York Smart Home (约克智慧家)」并重启 Home Assistant。

需要 Home Assistant 2025.3 或更新版本。已在 2025.3.4 和 2026.9.4 上跑过全部测试。

从 0.1.x / 0.2.0 升级时会自动迁移配置，实体 ID 保持不变。

## 配置

设置 → 设备与服务 → 添加集成 → 搜索「York Smart Home」，选择登录方式：

- **手机号 + 密码（推荐）**：登录失效时可以一直自动续期。
- **短信验证码**：适合没有设置密码的账号，靠 refreshToken 续期；续期失败时 HA 会提示重新登录。

插件会生成自己的推送 ID（`jgRegId`）并保存下来。服务器不接受空的 jgRegId；也不复用手机的推送 ID，免得 App 的推送被转到 HA 上。

登录失败时会显示云端返回的原因（例如密码错误、账号未注册），不会笼统地提示「密码错误」。

添加后可在集成的「选项」中修改：

- 轮询间隔（默认 2 秒，与 App 相同）；
- 总是抢回登录（默认关闭）和让出登录的时长（默认 30 分钟，可设 5~720 分钟），见下一节。

## 与手机 App 共用账号

约克智慧家账号同一时间只能在一处登录：手机 App 登录后，HA 的会话会失效（接口返回 HTTP 401，App 自己把它当作「该账号已在其他手机登录」，并且从不自动重新登录）。
以前的版本收到 401 会立即重新登录，结果是把手机 App 踢下线，App 还会关掉自动登录，每次都要手动重新输入。

现在默认「让出」：

- 收到 401 后 HA 不再重新登录，而是停止访问云端，等待「让出登录的时长」（默认 30 分钟）。期间实体在连续几次轮询失败后显示为「不可用」，并在「设置 → 修复」里出现提示。
- 时间到了自动重新登录（密码登录用密码，短信登录用 refreshToken），成功后提示自动消失，状态重新完整读取一次。如果因为云端故障（超时、5xx）登录失败，不会每次轮询都重试，而是等 1 分钟、2 分钟……最长 15 分钟再试。
- 让出期间修改选项立即生效：打开「总是抢回登录」会在下一次轮询时重新登录；调整让出时长会按新的时长重新计算结束时间。
- 控制指令在强制重新登录后又立刻被挤掉（手机马上又登录了），不会要求重新输入密码，而是提示指令未发送并开始让出。
- 想立即用 HA 控制：在修复提示里点「提交」立即重新登录；或者在实体还可用时（刚被挤掉的前几次轮询内）直接发控制指令，也会立即重新登录（明确的操作意图），重新读取状态后再发送。这两种情况都会把手机踢下线。在选项里打开「总是抢回登录」也会立即结束让出。
- HA 启动时如果账号正被手机占用，集成照常加载但暂时没有设备，等重新登录后设备自动出现，不会在启动时把手机踢下线。
- 密码错误等登录接口本身的失败仍然会提示重新登录（reauth）。

如果打开「总是抢回登录」，就恢复以前的行为：收到 401 立即重新登录；如果重新登录后 10 分钟内又被挤掉，就认为账号正在别处使用，停止自动登录并提示你在 HA 里重新登录，避免 HA 和 App 一直互相顶替。

**推荐做法：给 HA 单独用一个账号。** 用另一个手机号注册第二个账号，在 App 里用主账号进入「家庭管理 → 邀请成员」生成二维码，用第二个账号扫码加入家庭，然后在 HA 里用第二个账号登录。插件会读取账号下所有家庭的设备，加入的家庭里的线控器会自动出现。这样手机和 HA 都不会被踢下线。

## 接口说明（供维护参考）

所有请求都需要以下固定请求头，没有签名：

```
X-His-AppId: com.hisensehitachi.iyes2
X-His-APIKey: 61PdDpLyYzpctYwwVpbHRb2
X-His-AppTag: V1
X-His-Version / X-His-OS / X-His-Locale / X-His-Timestamp
Authorization: Bearer <token>
```

| 用途 | 请求 |
| --- | --- |
| 登录 | `GET /login?phoneNo=&password=&jgRegId=&version=0` → `data.user.token` |
| 短信验证码 | `POST /smsLogin/getVerifyCode`，`{"phoneNo":…}` |
| 短信登录 / 续期 | `GET /smsLogin?phoneNo=&jgRegId=&version=&verifyCode=` 或 `&refreshToken=`（续期时不带验证码） |
| 设备发现（全部家庭） | `GET /api/apphome/homes/homeList/deviceList` → `allHomeDeviceList[].xkqList` |
| 线控器详情（能力/锁定/限温） | `GET /api/appdevice/homes/{homeId}/xkqs/{xkqCode}` → `data.constantJson` |
| 状态 | `POST /api/appstatus/homes/{homeId}/status`，`{"homeId":…, "xkqListGjy":[{"xkqCode":…, "timestamp":0}]}` |
| 在线状态 | `POST /api/appcontrol/cmds/xkqOnlineStatus` |
| 控制 | `POST /api/appcontrol/cmds/multiIuOuCtrl`，`ctrlType:"YO420"`，`xkqCtrlList[].ctrlJson` |
| 控制结果 | `POST /api/appcontrol/cmds/multiCtrlResult` |
| 定时 / 取消定时 | `POST /api/appdevice/homes/{homeId}/xkqs/{xkqCode}/simpleTimer`，`{"timerSimpleMinute":…, "ctrlJson":{…}}` → `data.timerSimpleTime` / `data.systemTime` |

`ctrlJson` 字段：

| 操作 | ctrlJson |
| --- | --- |
| 开/关机 | `{"if_run":"1"}` / `{"if_run":"0"}` |
| 设定温度 27.5°C | `{"set_temp":27, "set_temperature_add05":"1"}` |
| 模式 | `{"wire_controller_makecold":"1"}` 制冷，`heating` 制热，`rmwet` 除湿，`makewind` 送风，`auto` 自动，`saveene_freshair` 节能新风，`dry_comfortable_rmwet` 干爽除湿，`strong_rmwet` 强力除湿，`auto_rmwet` 自动除湿，`floor_heat` 地暖，`rmwet_floor_heating` 除湿+地暖，`air_supply_floor_heating` 送风+地暖，`heat_floor_heating` 制热+地暖，`purification` 净化，`heating_mode` 供热（都带 `wire_controller_` 前缀） |
| 风速 | `{"wire_controller_wind_low":"1"}`，另有 `_mid` `_high` `_superlow` `_superhigh` `_light`(微风) `_auto` |
| 滤网复位 | `{"screen_mark_reset":"1"}` |
| 设置（整包） | `{"key_lock":"0", "key_sound_switch":"1", "indicator_light_mode":"1", "backlight_brightness_mode":"1", "indicator_light_brightness":30, "backlight_brightness":30}` |
| 设置（普通线控器） | `{"key_lock":"0", "backlight_brightness":3, "backlight_brightness_mode":"0", "key_sound_switch":"1"}` |
| 附加功能 | 所有可用功能一起发：`{"energy_conservation":"1", "sleep":"0", …, "self_clean":0, "hig_temperature_sterilization":0}` |
| 停止自清洁 | `{"self_clean":0, "if_run":"0"}` |
| 湿度 | `{"set_humidity":50}` |
| 导风板 | `{"position_of_air_deflector1":"0", "section_of_air_deflector1":"2", "air_deflector1_independent_or_together_ctrl":"0", …}`；3D/超远风 `{"three_air_supply":"1"}` / `{"ultra_far_air_supply":"1"}` |
| 单向人感 | `{"one_direction_human_sense":"2"}` |
| 独立人感 | `{"independent_human_sense_effect":"1", "independent_human_sense_state":"3", "independent_human_sense_nobody_judge_time":"1"}` |

能力位（`constantJson`）里的 `*_fix` 等字段表示设备是否支持对应的模式或风速；注意部分模式的能力位不是 `_fix` 形式（`saveene_freshair`、`is_dehumidification2_avalable`、`is_dehumidification3_avalable`、`is_floor_heating_avalable`、`controllerFloorRmwetFix`、`air_supply_floor_heating`、`controllerFloorHeatFix`、`is_purification_available`、`is_heating_mode_available`）。
账号在其他设备登录后接口返回 HTTP 401（App 只根据 HTTP 状态码判断 LOGIN_ELSEWHERE）；插件为保险起见，把响应体里的 `code:"401"` 也按同样方式处理；业务码 `code:"300"` 表示要求升级 App，`data.version` / `data.url` 是新版本号和下载地址。
App 的线控器详情页只看控制接口返回的 `code`：`200` 表示成功，其他值会把 `message` 提示给用户。
首页的快捷开关还会轮询 `multiCtrlResult`（`200` 完成，`205`/`400`/`500` 执行中，其他值失败），插件没有采用这一方式。

状态查询：`xkqListGjy[].timestamp` 填上一次拿到的状态的 `roundTime`（App 首页的做法），有更新的上报才会返回；填 0 表示读取当前状态。上报的 `otaFlg` 为 `"1"` 时 App 下一轮填 0。

锁定位：`run_model_fixed` 锁模式，`set_temp_fixed` 锁温度，`B9` 锁风量。

## 已知限制

- 目前只支持 Wi-Fi 线控器（`xkqList`）类设备。走 iEZ 网关的内机（`deviceList/iuList`）使用另一套协议，没有实现。
- 附加功能、湿度、导风板、人感、新风型线控器和普通线控器（`xkqType=8`）都是按 App 代码实现的，作者的设备没有这些能力，都还没在真机上验证过；有这些设备的用户欢迎提供诊断信息。导风板「自动」表示摆风是根据 App 图标推测的。
- 云端轮询，控制后状态同步有 2~4 秒延迟。
- App 把所有 401 都当作「在其他设备登录」。如果服务器在 token 自然过期时也返回 401，插件同样会先让出一段时间再重新登录；这种情况下可以把让出时长调短，或者打开「总是抢回登录」。
- 短信登录的 refreshToken 续期与 App 的自动登录调用方式相同，但还没在真实账号上验证过。
- 模式切换、扩展模式（地暖、净化等）、详情接口的 `constantJson` 格式还没在真机上完全验证过。字段来自 App 代码，不是抓包。
- 定时的请求格式来自 App 代码，还没在真机上抓包验证；`timerSimpleTime` 是毫秒时间戳是从 App 的用法推断的，定时进行中 `simple_timer_time` 上报什么、`255` 是否表示「没有定时」也还没确认。

## 开发 / 测试

```bash
pip install pytest-homeassistant-custom-component ruff
pytest
ruff check . && ruff format --check .
```

测试需要在 Linux/macOS/WSL 下运行，因为 Home Assistant 不支持 Windows。
