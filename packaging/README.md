# Telos Harness 插件（隔离预览版）

0.4.11 适用于 macOS 14+ Apple Silicon、官方 Node/CLI 或官网桌面版 DeepSeek Harness 0.2.1-rc.2。安装包包含 Python 运行环境和编译后的 Apple helper，不需要用户安装 Python、Homebrew 或 Xcode。

首次使用请先阅读 [项目首页](https://github.com/lucifersteps/telos-harness#readme) 和 [AI 安装指南](https://github.com/lucifersteps/telos-harness/blob/main/docs/install-for-ai.md)，遇到问题查看 [排障指南](https://github.com/lucifersteps/telos-harness/blob/main/docs/troubleshooting.md)。本文保留详细配置与功能说明；安装旧版本时请阅读对应 tag 的文档。

首次配置默认开启 Apple 日历与提醒事项，并引导用户授予两项 macOS 权限；百度地图和语音可按需开启。程序当前默认同时开启 Apple 和地图，没有百度 Token 时请在首次配置中关闭地图。暂时不授权 Apple 不影响先使用微信聊天，但 Apple 功能尚未就绪。

## 职责与依赖

Telos 只提供微信收发、Apple/百度地图工具、随包 Skill 内容、确认和防重复写入。DSH 提供循环、历史、模型连接、凭据、Skill 服务、任务清单和可选语音服务。插件只安装一个 Telos 入口，不修改宿主的全局模型、提示词、工具开关或语音选择。

微信会话通过 DSH 原生 setup 使用 Skill 加载器、Skill 工具、todo 工具及原生 web_search/web_fetch 工具；宿主未提供全局压缩服务时，在微信独立作用域挂载原生 compaction-basic 和工具结果修剪组件，沿用默认压缩阈值和超限恢复；这是该会话的原生能力配置，不另建 Skill 注册中心。提示词、业务工具和确认上下文只属于 Telos 创建的会话。微信新消息通过原生 steer 输入。

网页搜索与读取复用 DSH 的 web 服务、搜索供应商及凭据，Telos 不保存额外的搜索密钥。搜索后端不可用时由 DSH 返回原生工具错误；聊天模型可用不代表搜索后端已配置。

微信历史回查复用 DSH 原生 SQLite 全文索引及事件读取，只开放 session_event_search 和 session_event_read。执行层固定当前微信会话，拒绝跨会话目标；支持压缩前消息及相邻事件。索引在 Telos 独立作用域按需建立于内存，重启后从 DSH 原生日志重建，不另存聊天副本，也不改变宿主搜索设置。查询为字面全文匹配，建议使用短关键词；before/after 是原始事件数量，每侧最多 10 个。

宿主必须包含 `@deepseek-ai/dsh-base` 的标准服务；旧 `sdk-minimal` 不再支持直接运行此版本。缺少必要服务时 Telos 会明确报错，不自动补装。不启用语音也可以使用文字和图片。

## 桌面版

1. 在“插件 → 添加插件”安装 `telos-harness-0.4.11-macos-arm64.tgz` 并启用。
2. 在 Telos 页选择模型：跟随 DSH 默认模型、使用 DeepSeek 账号、选择已有 API 模型，或展开“添加其他 API 模型”。账号登录直接打开 DSH 原生入口；API 模型支持 OpenAI Chat Completions、Responses 与 Anthropic Messages 协议。
3. 在页面输入模型 API Key 或百度 Token，点击“保存到 DSH”。凭据直接调用 DSH credentials 服务，不经过 Telos 后端、不进入 Telos 配置或聊天，也不回显已存密钥。添加的 API Provider 通过 DSH settings 持久化；微信独立模型选择不会改变宿主默认模型。无模型列表时保存 Key 后刷新目录，也可手动填写准确的模型 ID。
4. 选择微信人设，保留 Apple 开启，百度地图按需开启，点击“保存并应用”。运行中应用配置会短暂重启 Telos，保留历史、待确认操作和防重复写入记录。
5. 微信扫码登录，授权 Apple 日历与提醒事项，再点击“启动 Telos”；以后打开应用或重新启用插件会恢复已登录实例。重新扫码和授权前需停止 Telos。暂不授权时可先验证聊天，并将 Apple 标为待授权。

“测试连接”只向所选模型发送一句测试文字，不加载人设、聊天或 Apple 数据，会产生少量模型调用费用。百度功能仍需单独的 `BAIDU_MAP_AUTH_TOKEN`，DeepSeek 账号不包含百度授权。关闭 Apple 会停止后台读取、撤下业务工具并拒绝确认写入；关闭地图会撤下地图工具与随包 Skill 目录。宿主另行安装的 Skill 仍归 DSH 管理，但不能绕过已关闭的执行工具。重新开启不会自动执行草稿，写入仍需用户确认。

百度凭据获取入口是 [百度地图 Agent Plan](https://lbs.baidu.com/apiconsole/agentplan)：登录百度账号，按页面指引获取 **SK（Service Key）**，填入桌面的“百度地图 Token”或 CLI 的 `BAIDU_MAP_AUTH_TOKEN`。普通地图 Web API、JavaScript API 或 MCP 的 AK 不适用于 Telos 这条接口。无需另外安装百度 MCP；额度与收费以官方页面为准。保存凭据并开启地图后，用一次明确地点查询验证实际调用；详细步骤见 [百度地图配置指南](https://github.com/lucifersteps/telos-harness/blob/main/docs/install-for-ai.md#baidu)。

CLI 继续使用 DSH 原生凭据服务或 `$DSH_HOME/.env`；不要把凭据放进聊天或 Skill 文件。`$DSH_HOME/telos/config.json` 可保存非敏感的 `model_selection`（provider、model、可选 reasoningEffort；null 表示跟随宿主）与 `tools`（apple、maps 布尔开关），重启后生效。旧配置未指定时保留原行为。

Apple 当前数据通过 personalPlan query/detail 按需读取，运行上下文不重复预载完整日历和提醒事项。待确认内容与实际回执仍在同一步更新，未变化的上下文由 DSH 原生机制去重。旧会话继续由 DSH 恢复和压缩，不删除历史或业务账本。

语音在 DSH 自身配置中启用和准备。Telos 不下载模型、不选择后端，也不自动补装语音服务。既有 DSH 模型文件保留原位。Telos 状态页只显示宿主语音状态。

通过桌面 CLI 替换安装包后，退出并重新打开 DSH，确认 Telos 恢复运行。插件列表中的新版本只说明安装文件已更新，不能证明旧业务进程和已加载的 JavaScript 已换代。升级验收同时核对进程启动时间及安装文件校验值。

通道仅投递模型实际回答、确认预览和操作回执。原生执行错误记录在会话与日志中，不再用固定“暂停/中断/已结束”文本冒充助手回答；没有实际内容时不发送消息。

禁用插件或退出应用会关闭收发和 Telos 会话。配置错误不会关闭整个桌面宿主。

## Node/CLI 版

使用 DSH 原生的独立 profile，由 `dsh-base` 提供基础服务，没有 Web/TUI 或 SDK JSON-RPC 入口。首次创建宿主配置：

```sh
export DSH_HOME="$HOME/Library/Application Support/Telos Harness"
mkdir -p "$DSH_HOME/profiles/telos"
```

仅在全新 CLI 安装时采用上述目录；已有安装沿用原 DSH_HOME、TELOS_HOME 和 profile。在 `$DSH_HOME/profiles/telos/package.json` 不存在时保存以下内容；已有文件只合并所需 bundle，不整份覆盖：

```json
{"name":"dsh-profile-telos","private":true,"dsh":{"profile":{"bundles":["@deepseek-ai/dsh-base"]}}}
```

由 DSH 配置模型和凭据。默认模型是 `deepseek-official / deepseek-flash`；可在此 profile 的 `cordis.patch.yml` 配置 DSH 的 `agent-default-model` 和 `llm-deepseek`。凭据可由 DSH 管理，或保存在它原生支持的 `$DSH_HOME/.env`（文件权限设为 600）：

```dotenv
DEEPSEEK_API_KEY=填写自己的模型密钥
BAIDU_MAP_AUTH_TOKEN=填写自己的地图令牌
```

安装包必须先下载并校验，下面 `/path/to/` 替换为实际绝对路径。安装并运行：

```sh
dsh plugin --profile telos add telos-harness@file:/path/to/telos-harness-0.4.11-macos-arm64.tgz --offline
dsh --profile telos --telos-setup
dsh --profile telos --telos-login
dsh --profile telos --telos-authorize
dsh --profile telos --telos-status
dsh --profile telos
```

`--telos-setup` 只创建 Telos 非敏感配置，不再要求输入 Key。首次配置保留 `tools.apple: true`，没有地图 Token 时在登录、启动前将 `tools.maps` 设为 `false`；只合并字段，保留其他配置。`--telos-prepare-voice` 已移除，语音由宿主准备。CLI 与桌面使用相同插件包。CLI 默认不启用录音界面、多 Agent 工具或额外服务器；微信会话接入 Telos 业务工具和原生 Skill、todo、网页、当前会话历史检索及定时任务工具。

最后一条命令持续运行，关闭进程会停止微信收发和调度。新终端需要重新设置同一个 DSH_HOME，以及已配置的 TELOS_HOME。`--telos-status` 显示配置、登录、权限和模型选择，不实际验证模型连通性或微信收发；安装完成后应收到微信测试回复，并成功只读查询日历和提醒事项。

如需本地语音，在 DSH profile 的 `cordis.patch.yml` 启用官方模块（不属于 Telos 安装声明）：

```yaml
- insert:
    - id: speech-to-text
      name: '@deepseek-ai/dsh-experimental-speech-to-text'
      config:
        defaultProvider: sensevoice-local
        language: zh
    - id: speech-to-text-sensevoice
      name: '@deepseek-ai/dsh-experimental-speech-to-text-sensevoice'
      config:
        dataRoot: !!js dshHomePath('speech-to-text', 'sensevoice')
```

模型仍须通过 DSH 的语音准备能力完成下载；上面的配置仅启用模块，不代表模型已就绪。也可以复用同一 DSH_HOME 下已准备好的原生模型。CLI 语音需要完整官方 Node 或桌面内置运行时，单文件精简运行时可能不包含所需资源。

## Apple 授权

点击桌面的「授权日历与提醒事项」或运行 CLI 的 `--telos-authorize`，macOS 会向独立组件「Telos Apple」请求日历与提醒事项访问权限。它仍是模型调用的 Apple 工具，只负责执行本机业务，不包含新的 Agent 或循环。两种 DSH 入口使用相同组件与授权方式，无需修改 DSH 的签名或权限声明。

状态页分别显示两项权限。若此前选择了拒绝，请在「系统设置 → 隐私与安全性 → 日历 / 提醒事项」中允许 Telos Apple，再检查状态；拒绝后系统未必重复弹窗。错误信息会区分权限不足、系统返回错误和等待超时。

授权、查询和确认后的写入统一由 LaunchServices 启动该应用；每次请求完成后退出，不安装常驻服务。请求内容通过仅当前用户可读写的临时文件传递，结束后清理，应用有最长运行时间。写入仍经过原有确认和账本；若执行结果未知，不自动重放。

当前开发包使用 ad-hoc 签名。本机验证：重启和升级时保持同一个已签名 Apple 组件，可保留授权；组件内容与签名改变后，系统重新要求授权。更换本次测试组件与正式安装包也可能需要再次授权。这属于代码身份变化，不能只凭相同应用名称保证授权延续。正式分发应使用稳定的 Developer ID 签名并完成公证；目前没有完成该分发验证。不要重置系统权限数据库来绕过授权。

## 微信人设

插件内置「默认助手」和「黎曼」，首次安装和没有人设字段的旧配置默认使用「默认助手」。黎曼保留行动力教练、平等交流和克制关怀的风格，去除原作者的专属称呼，不附带私人记忆或聊天记录；用户可以在微信中说明希望使用的称呼。

桌面版在 Telos 配置页选择「微信人设」。CLI 停止当前 Telos 实例后选择：

```sh
dsh --profile telos --telos-persona liman
# 恢复默认助手：
dsh --profile telos --telos-persona default
# 或选择自己的 UTF-8 文本文件（不超过 64 KiB）：
dsh --profile telos --telos-persona-file "/absolute/path/my-persona.md"
```

自定义文件也支持 `~/` 路径；文件保留在原处，插件只保存其路径。文件缺失、为空、编码不正确或超限时明确报错，不悄悄退回默认人设。不要编辑安装包中的文件来自定义，以免升级覆盖；使用自定义文件选项即可。

选择保存后重新启动 Telos，即作用于新建和恢复的微信会话，不改变其他 DSH 会话。历史、待确认草稿和操作回执保留；切换人设不会删除旧聊天中的措辞和称呼，也不会自动批准操作。人设与业务规则合成当前微信会话的完整提示词，不继承宿主其他会话的人设。身份和表达风格可变，参数校验、确认、防重复写入仍由业务代码执行。

所选人设会作为提示词发送给 DSH 当前使用的模型。自定义文件不要包含密钥或不希望发送的资料。升级保留 `config.json` 中的选择和自定义文件路径。

## 数据、升级与确认

数据默认位于 `$DSH_HOME/telos`，可用 `TELOS_HOME` 指定业务目录。历史、附件、凭据和语音模型由 DSH 保存；Telos 保存草稿、确认、Apple 操作账本及微信登录与收发回执。微信登录信息属于渠道业务状态。

0.2.x 升级前停止该隔离实例，备份整个 DSH_HOME，在 DSH 配置同样的模型和默认选择。旧 Telos `config.json` 中的 `model`、`base_url` 会保留但不再使用，插件不会自动修改宿主设置或迁移密钥。CLI 从 sdk-minimal 切换到上述 base profile，保留同一个 DSH_HOME，即可复用原生凭据、历史和 Telos 数据。不要同时启动两个微信实例。

升级只替换插件文件；不重新扫码、不覆盖已有业务数据。停止或卸载 Telos 不删除 DSH 的凭据、Skill 或模型。新版本拒绝不支持的配置版本。插件不安装 launchd 服务、不接管其他微信机器人。

Apple 写入仍须用户文字确认、预览送达与版本校验，并保留防重复写入账本。图片交给 DSH 原生附件和模型视觉能力；语音经 Telos 下载、解密并转为 WAV 后调用 DSH 已配置的识别服务。识别失败不猜测音频内容，语音不能代替文字确认。Telos 临时媒体处理后删除，进入 DSH 的图片保留遵循其原生规则。

安装包包含可选的通用黎曼人设，不含个人密钥、微信状态、个人资料或历史。当前为 ad-hoc 签名，尚未做 Developer ID 公证或另一台实体 Mac 验证。现有微信机器人不会自动切换。

## 微信定时任务

使用 `schedule_create`、`schedule_list`、`schedule_update`、`schedule_delete` 管理当前微信会话的任务。支持延迟、指定时间、固定间隔、每日、每周和 cron；日历规则使用明确时区。任务触发后仍通过原来的模型和 Telos 工具执行，并将回复送回原微信会话。创建 Apple 日历/提醒事项仍需原有人工确认，定时消息不能代替确认。

插件在两种宿主上均挂载独立的 DSH 原生调度服务，沿用原生规则、存储 schema、持久化和到期投递。只为微信会话提供恢复适配；已保存的账号或允许接收人变化时，拒绝恢复或向旧接收人发送。无需额外安装 Web 服务，也无需调度 API Key。原生存储域为 `telos_schedule_<业务目录哈希>`，存于 DSH 的 storage backend；业务目录和 DSH_HOME 应保持稳定。微信任务通过聊天管理，与桌面普通会话任务列表隔离。

DSH 和 Telos 必须保持运行。停止或禁用 Telos 会停止它的调度实例；重新启动后由原生引擎处理逾期任务。任务数据不因停止而删除。切换数据目录不会自动迁移任务，插件不安装系统常驻服务。

已完成的定时回复先保存到业务数据库，再逐段发送。已获成功回执的分段不会在普通重试中重复发送；失败后只重试发送，不重跑模型。重启时会读取原生会话日志，补齐尚未交给发件箱的已完成输出。远端已收取但本地未收到回执的网络中断窗口仍无法保证严格只送达一次。

## 用户记忆

每个微信用户的文件位于 `$TELOS_HOME/users/<hash>/user.md`（默认 `$DSH_HOME/telos/users/<hash>/user.md`）。哈希由渠道、租户和用户标识生成，模型不能选择用户或文件路径；同一用户的不同会话共享资料，不同用户和租户隔离。旧会话在下次收到消息时通过 DSH 原生工作目录变更切换到自己的目录，原会话历史继续恢复。

Agent 通过 `user_memory` 的 read/write 维护小型 Markdown 文件；write 提交完整新内容及上次读取的 expected_content，空内容表示清空。上限为 1,375 个 Unicode 码点，包含格式与空白；超限返回错误，由 Agent 合并精简后重试，不自动丢弃内容。DSH 原生文件锁和原子写入用于避免并发覆盖。

文件由 DSH 的 agent-instructions 加载器独立检查，内容变化才在消息末尾追加新版；时间、pending、receipt 不触发它重复注入。旧版本保留在历史中，新版声明优先；正常压缩移除文件上下文后会重新加载当前文件。该文件在模型中作为用户偏好指引使用，不能覆盖系统、开发者或用户当前明确指令。仅记录稳定事实、偏好及用户要求保留的信息，不把每轮对话、临时任务或 Apple 快照写入。

文件更新成功不等于历史日志删除：要求忘记会删除文件中的资料，但已有会话仍遵循 DSH 的历史保留规则。用户可以手工编辑文件；工具字符上限约束工具写入，Desktop 的原生加载另有 8 KiB 的上下文预算。CLI 已有全局文件加载器时复用它的配置，以 AGENTS.md（或宿主配置的候选名称）到 user.md 的符号链接接入；没有第二份资料副本，也不改宿主全局设置。没有定时整理、额外模型调用或压缩参数覆盖。

## 发布元数据

仓库根目录 `package.json` 和 `cordis.patch.yml` 声明 Telos 的正式身份与入口；`plugin/package.json`、`plugin/cordis.patch.yml` 是源码回归测试使用的隔离宿主夹具。构建脚本读取根目录元数据，把 `./plugin/` 模块路径映射到归档根目录，并打包 Python、Apple 和 SILK 组件。

源码克隆不含这些二进制，不支持直接作为用户安装包。插件市场应通过 `tarball` 字段指向 GitHub Release 中的预构建 `.tgz`。`engines.dsh` 只声明已验证的 `0.2.1-rc.2`；扩大范围前需验证新宿主。此次元数据整理不重建或替换已经发布的 0.4.11 附件，新元数据随下次构建进入安装包。
