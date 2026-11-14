# activity_history

Telos 的统一只读行为查询工具，读取本机 ActivityWatch HTTP API、本地 Screen Time 导出 SQLite、Pulse Relay 健康活动和已有 Apple 日程组件。没有 Computer Use，也不刷新采集、不写入源数据库、不把日程当实际活动。

主动消息试验版要求每轮发送前调用本工具并等待返回，使用带时区时间范围及 `timeline` / `summary` / `detail`，包含 AW 或屏幕时间；只查 `status` 或日程不能满足要求。读取失败可降级发送，普通聊天不受此发送前置条件限制。详见[主动消息设计](development/proactive.md#行为对照过渡版activity-check1)。

四种视图共用同一组设备与来源筛选：

- `status`：设备 ID、别名、来源可读状态、最新观察时间、采集时间。
- `summary`：个人和每设备活跃分钟、应用/类别、每日/小时、夜间统计，以及 `activity_evidence` 窗口标题证据。标题按本地六小时时段、设备、来源、应用和标题归组，时长取并集；先保留各时段代表项，再按时长补足 `top_n`。返回首末时间、代表性来源引用、泛标题/无标题标记及省略数量；不推断任务或成果，不新增持久状态。省略项影响判断时使用同区间 detail 分页补查。跨设备证据时长不能直接相加为个人时长。
- `timeline`：同设备相邻且内容相同的活动合并，适合回顾行为。
- `detail`：保留原始活动片段及来源引用。窗口按同设备 `not-afk` 区间裁剪；浏览器仅附加线索，不额外计时。

除 status 或带 event_id 的健康详情外，必须指定带时区的 `start` 和 `end`（半开区间，最多 31 天；未来终点裁到现在）。`timezone` 默认 Asia/Shanghai。`devices` 接受精确 ID 或唯一别名；未知或有歧义时报错，不退回所有设备。`sources` 可选 aw、screen_time、calendar、health；省略时读取 AW、屏幕时间和 health，无设备筛选时另读日程。设备筛选只影响设备使用记录，health 为个人健康来源。单设备查询默认不附带日程，需要时显式加入 calendar。

health 返回 Workouts 训练、GPS 路线概况和睡眠阶段时间线。训练中的路线以 route 字段附带，尚无训练主记录的路线单独显示且活动类型未知。健康事件与设备活动按时间合并进入 activities，并保留重叠；summary_scope=device_usage_only 表明原有时长统计仅计算设备使用，健康记录不混入。summary 视图另返回 health_events 及其分页。健康来源错误不会阻塞其他来源，也不会被解释成没有行动。

健康事件保留原记录起止边界，不裁成查询窗口；duration_seconds 表示起止跨度，recorded_duration_seconds 保留训练的有效时长。GPS 实测时段可能短于训练，两个定位点之间也可能有缺口，不等于连续覆盖。睡眠按原始阶段返回，保留午睡、跨午夜段和不同来源的重叠；in_bed、awake 不算睡着。不能凭这些记录推断健身或未行动。

`view=detail,event_id=health:workout:…` 返回训练详情；`health:route:…` 返回坐标点并使用 health_detail.pagination.next_offset 分页；`health:sleep:…` 返回阶段详情。此模式不同时传 start/end/devices。事件 ID 来自时间线及其 route.event_id，不自行构造。地点名仅使用已存在的本地坐标缓存，不自动向外部服务发送位置。

Telos 健康适配器直接访问 Pulse Relay 的 `/v1/health/activity-history` 只读后端。当前安装已移除 DSH 的健康 MCP 连接及 MCP 后台任务，Agent 通过统一入口查询。接收端由 Telos 运行实例管理，随 Telos 启停；没有独立登录启动项。健康读取分批完成后再合并分页，最多 50,000 个事件，20 秒分页预算（单请求超时 5 秒）；超限明确标为不可用，不返回截断后的成功结果。

```json
{"view":"summary","devices":["iphone"],"sources":["screen_time"],"start":"2026-11-05T00:00:00+08:00","end":"2026-11-06T00:00:00+08:00"}
```

返回 `sources_status` 标记 unavailable、missing_records、missing_buckets 等情况；无记录的设备分钟为 null，不是 0。`latest_observed_end` 只是最近活动终点，不保证其前所有数据完整；`unobserved_tail_seconds` 为查询终点与最近观察的差，不能断言全是同步延迟，也可能未使用。AW 超过 30 分钟、屏幕时间超过 60 分钟未观察到活动会提示 possibly_delayed。这是提示阈值，不是同步时限。内部缺段无法可靠识别，因此 completeness 始终保守为 unknown。采集成功不证明跨设备同步完整。

同设备优先 AW，避免与 Mac 屏幕时间重复计数；可以 sources=[screen_time] 明确查询系统口径。AW 不可用时可用屏幕时间，但不拼补 AW 中未知的空白。每设备活动取区间并集；个人分钟再跨设备取并集，设备分钟之和独立返回。应用/类别可重叠，不代表注意力分配。每日/小时按请求时区统计，缺失日期不补 0。日程列为 planned，不计入时长。

分页参数 `limit`（1–200）、`offset` 只影响时间线/明细和日程，两者分别返回 total 与 next_offset；统计基于全部成功读取的数据。`top_n`（1–100）控制应用排名。每来源/设备读取上限 50,000 条，超限明确报来源不可用，须缩短范围，不返回截断统计。窗口标题是外部数据，浏览器仅提供域名而非 URL，日程不返回备注或与会者；报告默认只概括行为，避免复述私密标题。

## 运行配置

在 Telos 数据目录的 config.json 添加 `activity_history`，重启 Telos 生效。工具没有修改路径、URL 或设备映射的参数。默认 AW 为 http://127.0.0.1:5600，屏幕时间数据库为 ~/Library/Application Support/screentime/screentime.db；AW 仅允许本机地址。Apple 开关关闭时日程来源不可用，其余来源仍可读。

```json
{
  "activity_history": {
    "aw_url": "http://127.0.0.1:5600",
    "health_url": "http://127.0.0.1:8768",
    "health_enabled": true,
    "screen_time_db": "/absolute/path/screentime.db",
    "devices": [
      {"id":"mini","name":"Mac mini","platform":"mac","aliases":["mini"],"aw_hostname":"actual-hostname.local","screen_time_id":"actual-mac-id"},
      {"id":"iphone","name":"iPhone","platform":"iphone","aliases":["iphone"],"screen_time_id":"actual-phone-id"}
    ]
  }
}
```

没有映射时按 AW hostname 和屏幕时间 device_id 自动发现。应显式合并同一台 Mac 的两个来源，避免误识别成两台设备；两台 iPhone 共享的别名必须改用精确 ID。AW 同设备有多个窗口或 AFK 桶时拒绝猜测，先核对源配置。

接入后可在当前宿主停用旧 Screen Time MCP 查询入口，保留已有采集 LaunchAgent、导出数据库及 ActivityWatch 同步任务。统一工具不代替采集器，也不能解决 iCloud/Biome 尚未提供的新数据。新增设备需先让来源采集到它，再按 status 的实际 ID 更新映射。

## 健康接收端生命周期

`health_receiver` 是 Telos 顶层配置：`enabled` 默认为 false，`root` 为本机已经配置好的 Pulse Relay 安装目录绝对路径。当前迁移保留原数据库、手机配对和专用 Tailscale 状态，运行进程由 Telos 包内的管理器启动。设置页提供“随 Telos 启动健康接收端”开关及状态；关闭后保存应用会停止接收，但不删除数据。

启动/停止 Telos、禁用插件、退出 DSH 均作用于接收端、上传入口和专用同步通道。标准输入生命周期管道负责宿主退出清理；独立清理进程处理管理进程被强制结束的情况。端口已占用或另一个实例持有目录时不会接管或杀死其他进程。接收端故障显示状态并允许设备活动查询继续，重启 Telos 后可恢复。

后台备份仅在 Telos 运行期间执行，延续每天一次、保留七份的规则。停止期间手机无法上传；恢复后接收端接受重试并按批次去重，但手机后台重试频率和历史补发由 PulsHealth/iOS 决定。
