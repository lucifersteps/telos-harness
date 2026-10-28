# Telos Harness 插件

0.4.16 适用于 macOS 14+ Apple Silicon、官方 Node/CLI 或官网桌面版 DeepSeek Harness 0.2.1-rc.2 / rc.3。安装包包含 Python 运行环境和编译后的 Apple helper，不需要用户安装 Python、Homebrew 或 Xcode。

本版修复提醒事项版本误冲突、新版 macOS 分区读取，以及微信保存卡和预览粘连、反馈后无法确认的问题；复用已有草稿、确认及消息记录，没有新增状态机。从 0.4.15 升级后 Apple 组件可能要求重新授权，按系统提示由用户允许日历和提醒事项访问。

首次使用请先阅读 [项目首页](https://github.com/lucifersteps/telos-harness#readme) 和 [AI 安装指南](https://github.com/lucifersteps/telos-harness/blob/main/docs/install-for-ai.md)，遇到问题查看 [排障指南](https://github.com/lucifersteps/telos-harness/blob/main/docs/troubleshooting.md)。本文保留详细配置与功能说明；安装旧版本时请阅读对应 tag 的文档。

首次配置默认开启 Apple 日历与提醒事项，并引导用户授予两项 macOS 权限；百度地图和语音可按需开启。程序当前默认同时开启 Apple 和地图，没有百度 Token 时请在首次配置中关闭地图。暂时不授权 Apple 不影响先使用微信聊天，但 Apple 功能尚未就绪。

## 职责与依赖

Telos 只提供微信收发、Apple/百度地图工具、随包 Skill 内容、确认和防重复写入。DSH 提供循环、历史、模型连接、凭据、Skill 服务、任务清单和可选语音服务。插件只安装一个 Telos 入口，不修改宿主的全局模型、提示词、工具开关或语音选择。

微信会话通过 DSH 原生 setup 复用宿主通用工具：桌面使用标准 Agent 预设，CLI 继承全局工具，包含文件读写、编辑、搜索、终端、后台作业、网页、Skill 与任务清单。宿主新增或禁用的工具沿用宿主设置，文件及终端操作继续经过原生权限与沙箱；工具复用不会授予额外系统权限。桌面问答、计划审批、展示面板、跨会话检索、工作目录切换、子 Agent/工作流和自主目标仍需额外渠道适配，当前不向微信开放。Apple 写入继续使用 personalPlan 的预览、保存与撤销策略。

压缩复用宿主服务或标准预设的原生组件；未提供时，在微信独立作用域挂载原生 compaction-basic 和工具结果修剪组件，沿用默认压缩阈值和超限恢复，不另建模型循环。提示词、业务工具和确认上下文只属于 Telos 创建的会话。微信新消息通过原生 steer 输入，后台作业完成通知复用定时输出的持久投递与去重，并作为非人工输入处理，不能代替用户确认写入。

网页搜索与读取复用 DSH 的 web 服务、搜索供应商及凭据，Telos 不保存额外的搜索密钥。搜索后端不可用时由 DSH 返回原生工具错误；聊天模型可用不代表搜索后端已配置。

微信历史回查复用 DSH 原生 SQLite 全文索引及事件读取，只开放 session_event_search 和 session_event_read。执行层固定当前微信会话，拒绝跨会话目标；支持压缩前消息及相邻事件。索引在 Telos 独立作用域按需建立于内存，重启后从 DSH 原生日志重建，不另存聊天副本，也不改变宿主搜索设置。查询为字面全文匹配，建议使用短关键词；before/after 是原始事件数量，每侧最多 10 个。

宿主必须包含 `@deepseek-ai/dsh-base` 的标准服务；旧 `sdk-minimal` 不再支持直接运行此版本。缺少必要服务时 Telos 会明确报错，不自动补装。不启用语音也可以使用文字和图片。

## 桌面版

1. 在“插件 → 添加插件”安装 `telos-harness-0.4.16-macos-arm64.tgz` 并启用。
2. 在 Telos 页选择模型：跟随 DSH 默认模型、使用 DeepSeek 账号、选择已有 API 模型，或展开“添加其他 API 模型”。账号登录直接打开 DSH 原生入口；API 模型支持 OpenAI Chat Completions、Responses 与 Anthropic Messages 协议。
3. 在页面输入模型 API Key 或百度 Token，点击“保存到 DSH”。凭据直接调用 DSH credentials 服务，不经过 Telos 后端、不进入 Telos 配置或聊天，也不回显已存密钥。添加的 API Provider 通过 DSH settings 持久化；微信独立模型选择不会改变宿主默认模型。无模型列表时保存 Key 后刷新目录，也可手动填写准确的模型 ID。
4. 选择微信人设，保留 Apple 开启，百度地图按需开启，点击“保存并应用”。运行中应用配置会短暂重启 Telos，保留历史、待确认操作和防重复写入记录。
5. 微信扫码登录，授权 Apple 日历与提醒事项，再点击“启动 Telos”；以后打开应用或重新启用插件会恢复已登录实例。重新扫码和授权前需停止 Telos。暂不授权时可先验证聊天，并将 Apple 标为待授权。

“测试连接”只向所选模型发送一句测试文字，不加载人设、聊天或 Apple 数据，会产生少量模型调用费用。百度功能仍需单独的 `BAIDU_MAP_AUTH_TOKEN`，DeepSeek 账号不包含百度授权。关闭 Apple 会停止后台读取、撤下业务工具并拒绝确认写入；关闭地图会撤下地图工具与随包 Skill 目录。宿主另行安装的 Skill 仍归 DSH 管理，但不能绕过已关闭的执行工具。重新开启不会自动执行旧草稿；新请求按当前写入模式处理。

百度凭据获取入口是 [百度地图 Agent Plan](https://lbs.baidu.com/apiconsole/agentplan)：登录百度账号，按页面指引获取 **SK（Service Key）**，填入桌面的“百度地图 Token”或 CLI 的 `BAIDU_MAP_AUTH_TOKEN`。普通地图 Web API、JavaScript API 或 MCP 的 AK 不适用于 Telos 这条接口。无需另外安装百度 MCP；额度与收费以官方页面为准。保存凭据并开启地图后，用一次明确地点查询验证实际调用；详细步骤见 [百度地图配置指南](https://github.com/lucifersteps/telos-harness/blob/main/docs/install-for-ai.md#baidu)。

CLI 继续使用 DSH 原生凭据服务或 `$DSH_HOME/.env`；不要把凭据放进聊天或 Skill 文件。`$DSH_HOME/telos/config.json` 可保存非敏感的 `model_selection`（provider、model、可选 reasoningEffort；null 表示跟随宿主）与 `tools`（apple、maps 布尔开关），重启后生效。旧配置未指定时保留原行为。

Apple 当前数据通过 personalPlan query/detail 按需读取，运行上下文不重复预载完整日历和提醒事项。待确认内容与实际回执仍在同一步更新，未变化的上下文由 DSH 原生机制去重。旧会话继续由 DSH 恢复和压缩，不删除历史或业务账本。

语音在 DSH 自身配置中启用和准备。Telos 不下载模型、不选择后端，也不自动补装语音服务。既有 DSH 模型文件保留原位。Telos 状态页只显示宿主语音状态。

通过桌面 CLI 替换安装包后，退出并重新打开 DSH，确认 Telos 恢复运行。插件列表中的新版本只说明安装文件已更新，不能证明旧业务进程和已加载的 JavaScript 已换代。升级验收同时核对进程启动时间及安装文件校验值。

通道仅投递模型实际回答、确认预览和操作回执。原生执行错误记录在会话与日志中，不再用固定“暂停/中断/已结束”文本冒充助手回答；没有实际内容时不发送消息。

普通聊天按完整想法分泡；列表标题与同一份列表保留在一条消息中，项目、缩进说明、子项目和代码块保留内部换行。操作预览与保存卡片仍完整独占一条消息。普通回复和定时回复使用相同规则，超过微信单条长度限制时才进一步分段。

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
dsh plugin --profile telos add telos-harness@file:/path/to/telos-harness-0.4.16-macos-arm64.tgz --offline
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

授权、查询和写入统一由 LaunchServices 启动该应用；每次请求完成后退出，不安装常驻服务。请求内容通过仅当前用户可读写的临时文件传递，结束后清理，应用有最长运行时间。写入遵循当前直接写入/确认策略，并保留原有操作账本；若执行结果未知，不自动重放。

当前安装包使用 ad-hoc 签名。本机验证：重启和升级时保持同一个已签名 Apple 组件，可保留授权；组件内容与签名改变后，系统重新要求授权。更换本次测试组件与正式安装包也可能需要再次授权。这属于代码身份变化，不能只凭相同应用名称保证授权延续。正式分发应使用稳定的 Developer ID 签名并完成公证；目前没有完成该分发验证。不要重置系统权限数据库来绕过授权。

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

数据默认位于 `$DSH_HOME/telos`，可用 `TELOS_HOME` 指定业务目录。历史、图片附件、凭据和语音模型由 DSH 保存；Telos 保存草稿、确认、Apple 操作账本、微信登录与收发回执，以及接收的文件原件。微信登录信息属于渠道业务状态。

0.2.x 升级前停止该隔离实例，备份整个 DSH_HOME，在 DSH 配置同样的模型和默认选择。旧 Telos `config.json` 中的 `model`、`base_url` 会保留但不再使用，插件不会自动修改宿主设置或迁移密钥。CLI 从 sdk-minimal 切换到上述 base profile，保留同一个 DSH_HOME，即可复用原生凭据、历史和 Telos 数据。不要同时启动两个微信实例。

升级只替换插件文件；不重新扫码、不覆盖已有业务数据。停止或卸载 Telos 不删除 DSH 的凭据、Skill 或模型。新版本拒绝不支持的配置版本。插件不安装 launchd 服务、不接管其他微信机器人。

普通单项新增/修改默认直接写入并回执，删除、批量及复杂操作仍要求预览送达和文字确认；所有写入保留版本校验和防重复写入账本。图片交给 DSH 原生附件和模型视觉能力；语音经 Telos 下载、解密并转为 WAV 后调用 DSH 已配置的识别服务。识别失败不猜测音频内容，语音不能代替文字确认。Telos 临时媒体处理后删除，进入 DSH 的图片保留遵循其原生规则。

安装包包含可选的通用黎曼人设，不含个人密钥、微信状态、个人资料或历史。当前为 ad-hoc 签名，尚未做 Developer ID 公证或另一台实体 Mac 验证。现有微信机器人不会自动切换。

## 微信定时任务

使用 `schedule_create`、`schedule_list`、`schedule_update`、`schedule_delete` 管理当前微信会话的任务。支持延迟、指定时间、固定间隔、每日、每周和 cron；日历规则使用明确时区。任务触发后仍通过原来的模型和 Telos 工具执行，并将回复送回原微信会话。定时消息触发的 Apple 写入仍需人工确认，不使用直接写入模式。

插件在两种宿主上均挂载独立的 DSH 原生调度服务，沿用原生规则、存储 schema、持久化和到期投递。只为微信会话提供恢复适配；已保存的账号或允许接收人变化时，拒绝恢复或向旧接收人发送。无需额外安装 Web 服务，也无需调度 API Key。原生存储域为 `telos_schedule_<业务目录哈希>`，存于 DSH 的 storage backend；业务目录和 DSH_HOME 应保持稳定。微信任务通过聊天管理，与桌面普通会话任务列表隔离。

DSH 和 Telos 必须保持运行。停止或禁用 Telos 会停止它的调度实例；重新启动后由原生引擎处理逾期任务。任务数据不因停止而删除。切换数据目录不会自动迁移任务，插件不安装系统常驻服务。

已完成的定时回复先保存到业务数据库，再逐段发送。已获成功回执的分段不会在普通重试中重复发送；失败后只重试发送，不重跑模型。重启时会读取原生会话日志，补齐尚未交给发件箱的已完成输出。远端已收取但本地未收到回执的网络中断窗口仍无法保证严格只送达一次。

## 用户记忆

每个微信用户的文件位于 `$TELOS_HOME/users/<hash>/user.md`（默认 `$DSH_HOME/telos/users/<hash>/user.md`）。哈希由渠道、租户和用户标识生成，模型不能选择用户或文件路径；同一用户的不同会话共享资料，不同用户和租户隔离。旧会话在下次收到消息时通过 DSH 原生工作目录变更切换到自己的目录，原会话历史继续恢复。

Agent 通过 `user_memory` 的 read/write 维护小型 Markdown 文件；write 提交完整新内容及上次读取的 expected_content，空内容表示清空。上限为 1,375 个 Unicode 码点，包含格式与空白；超限返回错误，由 Agent 合并精简后重试，不自动丢弃内容。DSH 原生文件锁和原子写入用于避免并发覆盖。

文件由 DSH 的 agent-instructions 加载器独立检查，内容变化才在消息末尾追加新版；时间、pending、receipt 不触发它重复注入。旧版本保留在历史中，新版声明优先；正常压缩移除文件上下文后会重新加载当前文件。该文件在模型中作为用户偏好指引使用，不能覆盖系统、开发者或用户当前明确指令。仅记录稳定事实、偏好及用户要求保留的信息，不把每轮对话、临时任务或 Apple 快照写入。

文件更新成功不等于历史日志删除：要求忘记会删除文件中的资料，但已有会话仍遵循 DSH 的历史保留规则。用户可以手工编辑文件；工具字符上限约束工具写入，Desktop 的原生加载另有 8 KiB 的上下文预算。CLI 已有全局文件加载器时复用它的配置，以 AGENTS.md（或宿主配置的候选名称）到 user.md 的符号链接接入；没有第二份资料副本，也不改宿主全局设置。没有定时整理、额外模型调用或压缩参数覆盖。

## 微信文件读取

从 0.4.12 起支持文件读取。在微信发送或引用文件，并附上“提取会议安排”“总结这份材料”等要求。只发文件且上下文没有明确任务时，助手应简短确认收到并询问用途；已有任务则继续处理。后续可继续追问已接收的文件。

- Word `.docx`、Excel `.xlsx`：调用 DSH 官方 `office-docx` / `office-xlsx` 技能，使用宿主 Python 环境中的 python-docx / openpyxl 按需检查原文件，不预先转换成 TXT。Excel 公式、单元格类型等仍可检查；读取公式不等于重新计算公式。
- PDF：DSH 0.2.1-rc.2 的原生 `read` 只读 UTF-8，桌面 PDF 预览并不是 Agent 正文读取工具。本插件提供小型 `telos-pdf` 技能，经 DSH 原生 `bash` 调用 macOS PDFKit，按页读取文字层。每次最多 10 页、24,000 字符，支持单页字符偏移续读。扫描页没有文字时明确告知，不做 OCR；多栏、表格顺序及图片内容不保证完整，可发送关键页面截图。
- 文本与图片：复用 DSH 的 `read` / `read_image`，输出长度、分页及视觉能力遵循宿主。

原文件下载上限为 16 MiB，不执行附件中的代码。旧版 DOC/XLS、加密或损坏文档可能需要另存或解锁。无需新的解析 API Key；读取内容仍会通过正常对话发送给所选模型。

桌面端复用宿主已有的 `load_workspace_dependencies`。独立 CLI 若没有此工具，可通过 `DSH_PRIMARY_RUNTIME` 指向已准备好的官方运行环境，Telos 会挂载同一个原生工具；也可按官方 Office 技能使用用户已配置的 Python 环境。准备步骤见 [CLI Office 环境](../docs/install-for-ai.md#cli-office)。缺少依赖时应明确提示，不在微信对话中悄悄安装包。

原件以内容哈希命名，保存在 `$TELOS_HOME/users/<用户哈希>/documents/<会话哈希>/`，不另建文档索引或会话状态库；临时下载目录处理后删除。备份时包含原件目录；清理后无法继续读取这些文件，已进入 DSH 历史的内容仍遵循宿主的历史保留规则。

本次增加了微信 Agent 的原生 `bash`、`read`、`read_image` 能力。执行遵循 DSH 的沙箱与授权，不启用后台任务，也不自动扩大权限；原生文件沙箱主要限制写入，并不保证只能读取附件目录。提示词限定文档处理用途，但这不等于操作系统级的附件隔离。Apple 工具按当前写入模式执行；文件内容本身不构成用户的写入要求或确认。文件结果回传微信尚未接入。

## 本地新增：微信图片分享（0.4.17-local.3，未发布）

微信会话可通过 `send_wechat_image` 发送公网 PNG/JPEG/WebP 图片（输入及转换结果均最多 10 MiB）。WebP 使用 macOS 系统工具转换为 PNG，不需额外安装图片库。小红书分享先发送封面，再将标题、简短摘要和完整原文链接合并成一条文字消息；图片本身不是可点击链接卡片。

收件人固定为当前微信会话，工具没有指定其他收件人的参数。保留笔记链接中的访问参数，无法获取真实封面或链接时不编造。发送回执按会话和工具调用 ID 持久化；同一调用重放不重复发送。接口接受仅表示微信服务端接收，客户端是否已展示需用户确认。超时或部分成功不自动重发图片；图片下载或上传失败时可退回标题、摘要与链接。普通文件附件和本地图片路径暂未加入此工具。

微信文字消息不支持把 URL 隐藏在标题内。若来源提供真实短链接则优先使用；不伪造短链、不删掉原文所需的访问参数。接口返回 `prepare failed` 时提示需要新的微信消息刷新回复窗口，不将明确拒收当成成功。

## 发布元数据

仓库根目录 `package.json` 和 `cordis.patch.yml` 声明 Telos 的正式身份与入口；`plugin/package.json`、`plugin/cordis.patch.yml` 是源码回归测试使用的隔离宿主夹具。构建脚本读取根目录元数据，把 `./plugin/` 模块路径映射到归档根目录，并打包 Python、Apple 和 SILK 组件。

源码克隆不含这些二进制，不支持直接作为用户安装包。插件市场应通过 `tarball` 字段指向 GitHub Release 中的预构建 `.tgz`。`engines.dsh` 声明 `>=0.2.1-rc.2 <0.3.0-0`，允许从 0.2.1-rc.2 起的 0.2.x 候选版及正式版，排除 0.3.0 及更高版本（包括预发布版）。已验证 rc.2 / rc.3；同一范围内未来版本仍需回归，范围不代表逐版验证承诺。发布后的附件保持不变，更新通过新版本分发。


## 0.4.14 回复修复

直接保存后，完整卡片与同轮独立问题的简短答案分别发送，不再用卡片覆盖查询结果。保留回复【1】撤销及原有预览确认流程。

### 直接写入与撤销（0.4.13 起）

0.4.13 对新老用户默认开启直接写入；升级前请告知用户默认行为变化。已有配置未设置 `direct_write` 时视为开启；显式设置为 `false` 时继续保留关闭状态。

桌面版在 Telos 插件页的“写入方式”中打开或关闭“直接写入”，再点“保存并应用”。应用时会重启 Telos，保留登录、历史、草稿及回执。关闭后每次写入都先预览确认。

CLI 使用原来的 DSH_HOME、TELOS_HOME 和 profile，停止运行实例后执行：

```sh
dsh --profile telos --telos-direct-write off  # 恢复每次确认
# 或：dsh --profile telos --telos-direct-write on
dsh --profile telos --telos-status
dsh --profile telos
```

`--telos-status` 中的 `direct_write` 显示当前设置。配置文件中的同名布尔值也可手动修改，已有配置只合并该字段，重启生效；字段缺失视为开启。

直接写入仅覆盖单项新增、普通标题/备注/日期等可恢复修改。删除、批量、重复系列、附件、父子关系/分区等结构变更、会议参与者相关对象，以及不能可靠恢复原状的修改仍预览确认。多项请求使用批量预览；同一条消息最多自动保存一项。用户要求先预览时仍可先确认；缺信息照常追问。定时任务不会自动写入 Apple，旧的待确认草稿不会因开关切换而自行保存。

保存后收到一张完整卡片，底部提示“回复【1】撤销；需要修改时直接回复修改内容”。回复 `1` 撤销当前已投递的保存卡片；待确认预览中的 `1` 仍表示确认。也可发送 `撤销` 撤回最近一次直接保存。当前微信使用文字指令，没有新增可点击的撤销按钮。撤销新增会删除该次新建的对象，撤销修改会恢复所改字段。对象后来被修改/删除时拒绝自动覆盖；重复撤销不会继续回退上一条；最新回复已不是保存卡片时，`1` 不会寻找更早的卡片。已触发通知无法收回，复杂操作不承诺自动回滚。取消尚未保存的预览仍发送 `取消`，已保存对象需用撤销或明确提出修改。

原值与保存结果记录在既有本地操作账本中，不另建任务数据库。不要清空数据目录；撤销依赖保存记录。遇到未知写入/未知撤销结果时先查询核实，不能反复创建或自行重放。

## 0.4.13 Apple 扩展

新增提醒事项标签、分区、子任务、图片/网页附件、紧急状态、序号星期重复与组合筛选；提醒事项只支持一个提前提醒。新增日历容器管理、日程出行时间及会议详情只读字段。复杂操作仍需确认。扩展接口已在 macOS 26.2 实机验证，其他 macOS 版本尚未验证；不恢复旧任务数据库、不直接访问 Apple 内部 SQLite。
