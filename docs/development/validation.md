## 0.4.10 — DSH 原生微信定时任务（2026-10-20）

Telos 在隔离作用域直接挂载原生 `dsh-schedule` 与 `dsh-tool-schedule`。原生存储 schema、规则、定时器及 Session 投递不做重写；通过独立存储域避免桌面调度实例与 Telos 双重驱动同一任务。受限恢复接口只接受已持久化且仍属于当前账号/允许用户的微信会话，调用原有 open/setup 保留工具、记忆、历史与模型选择。停止 Telos 会释放其调度实例；微信任务不进入桌面普通任务列表。

原生轮次结束后先 flush 会话，再将定时回复交给持久化发件箱。按 session + 事件 seq 去重，发送按分段记录成功进度；失败只重试投递，不重跑模型。重启时读取原生完整事件补齐尚未交接的输出。用户消息与定时任务并发时按实际轮次返回，避免把下一轮定时回复当作用户请求的响应。定时消息不执行人工确认分支，生成的 Apple 预览仍需后续用户确认。

验证结果：

- 初轮桌面、CLI、安装包、steer、输出、历史、网页回归：39 passed，224.80s（`reports/schedule-regression.log`）。
- 最终代码的 CLI 调度、发送恢复、并发、确认和长历史回归：24 passed，73.99s（`reports/schedule-final-cli.log`）。覆盖冷启动后逾期触发、已完成输出从原生日志恢复且模型不重跑、账号变化拒绝恢复/发送、每日与固定间隔规则、跨会话查询/修改/删除限制，以及分段发送失败后重启续发。
- 最终 0.4.10 安装包：官方 dsh CLI 启动器 + 官方 Desktop Host，共 4 passed，57.56s（`reports/schedule-final-package.log`）。包括两个入口的定时触发与重启、桌面停止期间不执行、桌面根 schedule.catalog 保持为空、插件完整启停/禁用/卸载及安装包清单。

模型、微信、Apple 使用本地测试桩，未向真实微信发送测试消息。原生调度服务需 DSH/Telos 常驻；没有系统级唤醒或退出后执行保证。发送成功但回执丢失的网络窗口不承诺严格 exactly-once。

已使用官方插件管理器将本机 desktop profile 从 0.4.9 更新为 0.4.10，并正常退出/重新打开 DSH。安装的 index.mjs、schedule.mjs、history.mjs 与源码逐字节一致，业务组件与测试归档一致；新的 DSH Host 和 Telos serve 进程已运行，线上业务库已建立定时发件箱表。未改变用户配置、登录和会话历史。

## 0.4.9 — 当前微信会话历史检索（2026-10-20）

复用 DSH 原生 session-query-sqlite 与 tool-session-query，仅注册 session_event_search、session_event_read；执行层校验调用者并强制当前会话，拒绝显式跨会话目标。Telos 独立作用域使用 first-search 内存索引，不改宿主配置，不复制聊天存储；每次最多 10 个搜索结果、前后文每侧最多 10 个原始事件。

真实 CLI 和官方 Desktop Host + 0.4.9 归档验证：模拟模型触发原生压缩，从 shadowed 用户消息检索并读取原文及前后文；进程重启后中文关键词再次命中；同工作目录下其他会话的搜索和读取被拒绝；桌面宿主 openAt 仍为 never。加上网页、steer、记忆和安装包生命周期回归共 30 passed（152.46s，reports/history-regression.log）。外部模型与微信服务为测试桩，没有发送真实微信测试消息。

已使用官方插件管理器替换本机 desktop profile 为 0.4.9，并完全退出/重新打开 DSH。安装的 index.mjs、history.mjs 与测试源码逐字节一致，新 DSH Host 与 Telos serve 进程已启动。搜索为原生 FTS 字面匹配，不保证同义词语义召回。

## DSH 原生网页工具接入（2026-10-19）

微信 Agent 的 setup 显式加载 dsh-tool-web，启用 web_search/web_fetch；搜索超时沿用 DSH 标准配置的 60 秒。保留继承工具限制，搜索后端、凭据和网页读取服务继续由宿主提供。

官方 Desktop Host（重建安装包）与 CLI 原生运行时集成测试覆盖搜索→读取→模型获得结果、进程重启后恢复同一会话、工具定义不重复。使用确定性的测试搜索/读取供应商及模拟模型、微信服务；没有验证真实账号的公网搜索可用性，也没有向真实微信发消息。

网页工具、steer、桌面生命周期及 CLI 安装包回归：24 passed（132.79s，reports/web-integration.log）。搜索后端缺失时的原生错误与会话结束：1 passed（1.73s，reports/web-missing-provider.log）。已重建现有 0.4.7 本地归档用于测试，未替换用户正在运行的插件。

## 0.4.7 — 取消微信泡泡数量限制（2026-10-19）

移除提示词的 1～3 个区间，以及按行发送时的尾部合并；微信默认配置不再限制泡泡数量，旧分隔符发送路径也不会截掉第 9 个以后的内容。操作预览仍作为完整原文保留。

源码分泡、预览、失败投递回归 32 passed（24.35s，reports/bubbles-unlimited-source.log）。实际 0.4.7 安装包、隔离官方 Desktop Host、按行与旧分隔符路径回归 4 passed（19.30s，reports/bubbles-unlimited-package.log）：重启后的普通回复连续 12 个泡泡逐条完全匹配；预览前后各 12 行全部保留；预览独立发送、确认流程与记忆恢复正常。没有使用真实微信发送测试消息。

## 0.4.6 — 普通聊天分泡与完整预览（2026-10-19）

普通聊天使用既有按行拆分配置；业务层将当前操作预览原文作为发送参数传递，预览内部格式保留，前后聊天分开发送。模型仍返回普通文本，没有新增模型轮次、输出标记或持久化状态。超出普通分泡预算时合并尾部文字，避免丢失内容；微信单条长度保护沿用原发送路径。

全量回归初轮 261 passed、42 skipped、2 failed（reports/bubbles-regression.log）。一个失败暴露生产 sidecar 显式关闭按行拆分，已启用并重建；另一个是旧测试仍假设提示与预览共用一泡，已改为断言两个独立泡泡且预览原文完整。最终重建包在官方隔离 Desktop Host、CLI 安装升级与过早确认保护复验 10 passed（89.50s，reports/bubbles-final-package.log）。独立模型、微信、Apple 桩验证，没有向真实微信发送测试消息。

归档 SHA-256：f0c656197ca9ab0a52fe2084fc64a706cd63442a9df2b13118587ec958201fa6。重建 Python 组件，保留原 Apple 组件签名。

## 0.4.4 — 预览与追问单一展示（2026-10-19）

最新真实微信记录显示模型自行概括预览后，render 因未找到完整原文又追加一份。现在最终回答引用工具原文一次，投递层停止追加；原文缺失或重复时使用唯一工具正文。独立查询答案可与完整原文一同保留。未增加状态、输出标签、模型重试或自建循环。

官方 Desktop Host 使用真实 0.4.4 归档和隔离服务验证：1 passed（19.94s），涵盖预览唯一、重启后确认、追问唯一及现有生命周期。日志 reports/desktop-presentation-0.4.4.log。

源码回归 43 passed（79.54s），日志 reports/presentation-0.4.4-final.log。初轮的混合查询模拟回答未包含预览原文，按新规则回退后缺少查询答案；已将该模拟回答更新为原生工具原文加独立答案，保留“买菜”结果断言，并增加缺失/重复预览及追问回退测试。

完整退出旧宿主 60179、业务进程 60180 后离线安装并重启。新宿主 62930 于 19:57:37 启动，业务进程 62931 于 19:57:38 启动；已安装二进制和内置提示词与测试过的 0.4.4 产物逐字节一致。SHA-256 e5c57996ced62be4947182177d9c8aa1e9320bb477bc7d4e5256f634212951d0。

## 0.4.3 — 取消伪造状态回复并核对升级生效（2026-10-19）

19:34:52 最新一轮仍返回 CONTEXT_WINDOW_EXCEEDED。排查确认业务 PID 53494 自 17:47:39 起未更换；该轮上下文仍注入 40,255 字符的 external_state。CLI 只替换安装文件，运行中的组件和模块没有切换，0.4.2 的生产验证误将插件元数据版本当成运行版本。

通道不再把非 completed 结束、传输异常、恢复未完成输入或空回答改写成固定聊天文本。保留模型实际文字、业务预览和实际回执；无内容则不调用微信发送接口，将该输入标为已处理，避免恢复时伪造消息。原始执行错误仍由 DSH 会话记录，通道也保留诊断日志。

- 源码回归首轮 44 passed，1 failed；失败来自测试模拟器在 DSH 重试空回答时返回默认文字，已修正为所有重试均返回空内容。该测试文件最终 3 passed，包含保留部分回答、失败后正常展示预览、空内容与恢复不发送状态话术。
- 实际 0.4.3 包在官方 Desktop Host 的长会话压缩、重启恢复、原生错误不投递状态话术及确认写入验证：2 passed（28.80s），日志 `reports/desktop-output-0.4.3.log`。
- 完整退出旧 DSH 后安装 0.4.3 并重新启动：旧 PID 29926/29934/53494 已退出；新宿主 PID 60179、新业务 PID 60180 均于 19:41:10 启动。已安装 index.mjs 和业务二进制与已测试产物一致。
- 归档：`dist/telos-harness-0.4.3-macos-arm64.tgz`；SHA-256 `ff8378fc071909bba3b254933dfce2a7956cd4762f8f3361bdff1de086658300`。重建 Python 业务程序，保留既有 Apple 授权组件。

## 0.4.2 — 微信会话上下文与原生压缩（2026-10-19）

根因已通过官方 Desktop Host 的隔离环境复现。`dsh-web-app/cordis.patch.yml` 将全局 `compaction-basic` 和 `command-compact` 设为 disabled；标准预设在自身隔离作用域加载压缩组件。Telos 通过原生 agents.create/resume 直接创建微信会话，没有选用 UI 预设，原实现因而没有自动压力检查或超限恢复处理。只有 dsh-base 的 CLI 环境具备全局压缩，先前短会话回归没有覆盖这一差异。

同一段本地历史副本、同一 Desktop Host、同一本机模拟模型的对照：去掉本次作用域挂载时，两次输入只有两次普通模型请求，无 compaction 事件；启用修复后出现原生 prune、start、summary、end 事件，共三次模型请求（其中一次摘要），随后正常继续。副本仅用于本机模拟服务，未请求真实账号模型，未修改原会话。隔离宿主另有缺少 serviceVersion 的遥测组件警告，与此故障无关。

Telos 的 context 不再调用 Apple query，而只返回 pending、receipt、time；Apple 数据由现有 query/detail 工具读取。移除每步刷新时变化的 checked_at 和查询窗口，从而让 DSH 自己对未变化上下文去重。微信 scope 无全局 compaction 时，隔离挂载官方 BasicCompactionEngine 与 ToolResultPruner；已有宿主服务时不重复挂载、不覆盖策略。

- 源码定向回归：48 passed，覆盖原生上下文去重、连续工具读取新数据、确认回执、消息插入、全局/作用域两种原生压缩和压缩后重启恢复。
- 官方 Desktop Host 与实际归档回归：17 passed（123.85s），日志 `reports/desktop-context-0.4.2.log`，覆盖桌面长会话压缩、重启恢复、配置、工具开关及安装包确认流程。
- 0.4.2 文件安装与隔离验证通过，但当时生产生效验证不足：界面显示新版本不代表进程已换代。19:34 的后续失败仍由 17:47 启动的旧业务进程处理，继续注入完整 external_state 且没有压缩事件。0.4.3 的部署验收增加完整宿主重启及新业务进程启动时间核对。
- 0.4.2 包重新构建 Python 业务程序，复用 0.4.1 Apple 组件；三个 native 文件 SHA-256 全部一致。
- 归档：`dist/telos-harness-0.4.2-macos-arm64.tgz`；SHA-256 `c879db23a8e19baee4a8aad5c5beecde5b17c4feaf70448e8e9f2923d5ee3f67`。

## 0.4.1 — 统一配置页、模型选择与工具开关（2026-10-19）

- 最终安装包：`dist/telos-harness-0.4.1-macos-arm64.tgz`；SHA-256 `4f9dc6386c33766468687d22b2f6bd16576495d3c61eca91f17df0f305f962b8`。
- 模型目录、连接测试与 API Provider 注册调用原生 DSH llm/settings；账号登录调用原生 account；浏览器 API Key/百度 Token 直接进入 credentials，不经过 Telos Python、配置或模型历史。
- 微信模型选择及 Apple/地图开关使用既有 config.json；运行中“保存并应用”释放旧原生会话、保存后恢复。全局默认模型不变，确认和写入账本沿用原实现。
- 新增实际 Desktop Host 流程：添加自定义 API 模型、重启后测试连接、独立模型选择、关闭两类工具、无 Apple helper 仍正常聊天、错误模型不覆盖配置/不中断运行、重新开启地图、再次重启恢复人设与模型。所有模型/Apple/微信操作使用隔离模拟服务。
- 新增业务边界：关闭 Apple 后不读、不写、不核对账本，待确认草稿保留；重启、重新开启再确认只写一次；关闭地图拒绝过期工具调用。UI 回归验证模型/地图凭据只写 DSH，不进入 Telos 请求，失败不回显密钥，保留 Cordis 服务依赖检查。

验证结果：

- 完整 Node 回归首轮 `243 passed, 44 skipped, 1 failed`（235.42s）。唯一失败是旧人设测试要求系统提示词逐字不变，与新增工具开关说明冲突；修改为核对原业务规则保留并检查关闭说明后，专项与最终包复核均通过。44 项跳过为显式开启的真实服务/系统授权及桌面专项，不将其计为通过。
- 最终业务代码的官方 Desktop/包安装/人设/媒体适配/工具开关专项：`33 passed`（143.57s），日志 `reports/desktop-0.4.0-final.log`。0.4.1 与该最终 0.4.0 归档仅 README 和 package.json 版本不同；代码和二进制相同。
- 最终 0.4.1 包完整性、适配器、人设规则和工具边界复核：`10 passed`（1.13s）。
- 真实用户桌面：实际展示 0.4.1 卡片布局、精简模型菜单、API 配置字段、凭据输入、人设和开关；真实 DeepSeek Account / deepseek-flash 简短连接测试成功。实测切换到同一账号模型、关闭地图并保存应用、恢复跟随默认模型和原工具开关，均恢复运行。未发测试微信消息、未写真实 Apple 对象、未更换用户凭据。
- 同版本归档在 pnpm 中被缓存，最终改用 0.4.1 正常升级；已安装 client.js/models.mjs 的 SHA-256 与最终源码一致。真实页面显示 0.4.1、微信已登录、Telos 运行中、Apple 已授权。
- Apple 已签名组件与 0.3.4 完全相同，本次升级没有重新签名，也没有重新请求授权。当前 DSH 未存百度 Token；页面提供输入入口，未声称地图已连通。语音准备失败为既有宿主状态，本次未更改语音模型。

# 0.3.4 桌面配置区恢复（2026-10-19）

用户已安装 0.3.3，但 Telos 详情只有组件和来源，没有配置表单。在实际 DSH 桌面开发者控制台复现：`cannot get property "remote" without inject`，来源为 Telos client.js。0.3.3 声明了 remote.credentials，遗漏了访问 ctx.remote 所需的上层 remote 声明；Cordis 在渲染配置插槽时拒绝访问。

已补齐一项依赖声明。前端测试的上下文现在检查访问的服务必须声明，避免旧的普通对象 mock 掩盖此类问题。桌面内置运行时的页面/语音适配与包清单检查 3 passed（页面测试内部包含 3 项）。没有重新运行不受影响的全部业务回归；0.3.3 的 238 passed 及 26 项桌面 Host 测试并未覆盖真实前端的 Cordis 依赖访问，不能据此宣称当时配置页面可用。

使用桌面自带官方插件管理器将用户当前安装从 0.3.3 更新为 0.3.4，短暂禁用后重新启用；已在实际桌面上确认配置表单、人设、百度 Token 输入框、凭据“尚未配置”状态均显示，Telos 正在运行、微信已登录、Apple 两项已授权。保留原配置（当前默认助手）、账号和业务数据，未切换旧 Telos 机器人、未发送测试微信消息或写入 Apple 数据。没有填写/更换任何 Key。完整模型选择与工具开关仍未实现。

归档清单 67 个文件验证通过，所有 bin/ 和 native/ 文件与 0.3.3 完全一致。SHA-256：`6fed3476bc211baca58719f8c0b912d8b902ae26b22feab46b1fe1926496db67`。产物 dist/telos-harness-0.3.4-macos-arm64.tgz。未提交或推送 GitHub。

日志：reports/install-desktop-0.3.4.log、reports/desktop-0.3.4-client.log。通用 Node CLI 拒绝管理 desktop profile 后，改用官方桌面自带 CLI 成功更新；未绕过 profile 管理限制。

---

# 0.3.3 确认回执、人设表单与凭据入口验证（2026-10-19）

## 诊断与修复

- 运行中的桌面 Telos 0.3.2 配置没有 persona 字段，实际采用默认助手；界面选择未成为持久化配置。新版本启动时一并保存当前表单，并明确显示已保存/生效人设与未保存状态，不把选择等同于生效。
- 官方 DSH 0.2.1-rc.2 的原生顺序是 systemPrompt.assemble → agent/pre-step → 上下文投影。原接入在 pre-step 执行确认，但业务上下文已经读取旧状态，导致本轮模型说“正在提交”、下一轮才看到成功回执。现在先创建上下文段，在 pre-step 确认后填入最新值，再由 DSH 原生 next() 投影；没有复制模型历史或新增循环。
- 仅确认/取消且没有后续工具工作的轮次直接发送实际回执；同轮有独立工具工作或混合输入时仍保留其回复。提示词明确回执已完成，不在下一轮主动重复历史结果。原有确认、版本核对与操作账本不变。
- 当前桌面模型提供商是 deepseek-account，通过 DSH 账号登录可用；DeepSeek API Key 是另外的“设置 → 模型 → 编辑 DeepSeek”入口。百度未配置。新增页面入口直接调用 remote.credentials.describe/set，不经 Telos 配置接口或 Python 组件保存，不回读 Key、不展示凭据错误原文。

## 验证

- 原生确认/steer 定向首轮 9 passed、1 failed；失败是新测试误认为上下文含段落名称。改为解析实际 JSON 后确认用例通过，验证同轮 receipt 非空且 pending 清除、下一轮 receipt 为 null、只投递一次实际结果。
- 页面事件测试 3 passed：选择黎曼后直接启动携带最新表单；Token 仅提交 DSH 凭据服务；失败不回显密钥。
- 官方 Desktop Host 与安装包、人设、前端/语音适配专项：26 passed，132.50 秒，覆盖选择默认后启动时改为黎曼、原生模型收到黎曼提示、重启确认与重复写入保护。
- 完整 Node/CLI 回归：238 passed、43 skipped，231.38 秒；跳过项为未启用的真实模型/系统授权等专项，不计作通过。

产物 dist/telos-harness-0.3.3-macos-arm64.tgz，SHA-256：`94b9213a7f438fa42b9d908d41c1778227da992eb9d74ec19acaa1c83f6e6cad`；已核验清单中 67 个文件，Apple 应用所有文件与 0.3.2 完全相同，避免本次 Python/JS 修复改变 TCC 签名身份。仅重新构建业务组件，未重新编译/签名 Apple 应用。

本轮未切换生产微信、未写入真实 Apple 数据、未发送真实模型测试请求；没有替换用户当前桌面安装。完整模型选择与工具开关仍是后续讨论方案，未在本轮实现。

日志：reports/node-0.3.3-full.log、reports/desktop-0.3.3-regression.log、reports/confirmation-0.3.3-focused.log、reports/confirmation-0.3.3-corrected.log。

---

# 0.3.2 独立 Apple 授权验证（2026-10-19）

仅更新独立插件与隔离测试包，没有替换用户当前 DSH 中的 Telos 0.3.1，没有切换现有微信机器人，也没有提交或推送 GitHub。

## 实现与边界

- 原先直接启动 helper 时，macOS 把权限责任归到 DSH；桌面宿主缺少 Calendar entitlement，导致授权失败。现在所有真实 Apple 调用通过 LaunchServices 启动独立的 Telos Apple（com.telos.harness.apple）。
- 应用携带 Calendar entitlement 与日历、提醒事项用途描述；每次处理一个请求后退出，无额外 Agent、业务状态机或常驻服务。
- JSON 请求与回执使用 0700 临时目录、0600 文件，完成或失败后清理。应用使用原生期限限制生命周期；未知写入结果沿用已有 pending 账本，不自动重放。
- 桌面与 CLI 共用授权、状态和实际数据访问路径；显式外部 helper 覆盖保留给兼容和模拟测试。
- 未修改 DSH 签名、系统权限数据库或生产配置；未写入真实日程、提醒事项，未连接真实微信或调用模型 API。

## 验证结果

- Node/CLI 完整回归：236 passed、43 skipped，215.44 秒；跳过项包括未启用的外部模型与桌面专项等，不能计作通过。
- 官方桌面运行时专项：17 passed，86.69 秒，覆盖桌面配置/启停、安装升级、确认与防重复写入及新的传输失败保护。
- 真实 macOS 首轮授权、桌面重启、CLI 访问和同一已签名组件升级：1 passed，33.72 秒。用户在系统弹窗允许日历与提醒事项；EventKit 只读查询成功，不记录个人条目。
- 扩充后的真实升级用例：1 passed，96.12 秒。除上述流程外，实际改变组件内容并重新 ad-hoc 签名、升级安装，系统状态变为 not_determined；用户再次允许两项权限后恢复 full_access。配置保持不变。
- 新增传输用例覆盖临时文件权限和清理、超时、空回执、损坏 JSON、明确权限拒绝、兼容覆盖。未知回执保持 pending 且重复调用不会再次执行；明确权限拒绝不遗留已写入状态。

## 签名与分发限制

本机没有可用的 Developer ID 签名身份；当前包为 ad-hoc + hardened runtime。相同已签名组件的重启与包升级保留授权已实测，但组件更新后重新授权同样已实测。构建增加 --sign-identity 参数，正式 Developer ID 签名、公证与另一台实体 Mac 仍未验证。不能把“升级保留授权”泛化到当前所有开发包更新。

产物为 dist/telos-harness-0.3.2-macos-arm64.tgz，附独立 SHA-256 文件。最终文档打包复用已验证的组件，不重新编译或签名。 归档内 67 个文件的清单哈希全部通过，原生二进制与已验证构建一致，插件代码与源码一致；代码签名检查、JS 语法和 git diff --check 通过。最终 SHA-256：`e7e912818a5505c82a7e2d77c56c8f1ef29bb21263040babc4a20f8453fe3d50`。生产 telos-personal-main 工作区保持干净。

日志：reports/node-0.3.2-full.log、reports/desktop-0.3.2-regression.log、reports/apple-0.3.2-real-permissions.log、reports/apple-0.3.2-real-upgrade.log。

---

# 0.3.1 微信人设验证（2026-10-19）

本轮仅更新独立插件源码与隔离测试包，没有安装到用户正在使用的 DSH，没有切换现有微信机器人，也没有推送 GitHub。

## 实现

- `personas/liman.md` 随插件分发，保留既有黎曼身份、教练风格与平等交流偏好，删除原作者专属称呼；默认仍为通用助手。
- 桌面页提供默认助手、黎曼、自定义文件；CLI 使用 `--telos-persona` 或 `--telos-persona-file`。选择持久化到现有 config.json。
- 人设只组合进微信会话的完整提示词；原生 Agent setup/resume、历史内 system 更新仍由 DSH 管理，没有新增循环、记忆或状态机。
- 切换须停止并重新启动 Telos；确认草稿、历史、微信游标和操作账本保留。自定义文件须为非空 UTF-8 文本且不超过 64 KiB；无效输入不改配置，启动时文件失效会明确报错。

## 验证结果

- Node 完整回归首次为 229 passed、42 skipped、1 failed；失败位于新增人设测试的协议读取断言。修正后重跑全部 13 项人设测试，13 passed（23.91 秒）。其余已通过用例无需重跑。
- 桌面安装、配置、重启、会话隔离、升级与人设测试首次为 23 passed、1 failed；同样是上述断言。官方桌面 Host 的黎曼加载、重启及独立会话隔离在首轮已通过；修正后单独重跑恢复用例为 1 passed（11.87 秒）。
- 失败原因：DSH 原生 DeepSeek Messages 使用顶层 system，并在恢复会话后通过历史内 system 消息更新人设；测试起初只读 Chat Completions 的消息字段，随后又只读了缓存保留的初始 system。最终检查最新的完整 system 提示词，保持对身份切换、业务规则和写入结果的断言。
- 完整人设流程覆盖：选择黎曼 → 错参纠正 → 生成预览但不写入 → 停止 → 选择自定义文件 → 恢复同一会话并确认 → 重复确认不再写入 → 切回默认助手。另覆盖非法选择、缺失/空/超限/编码错误文件、不覆盖配置与缺失文件恢复。
- 归档清单 67 个文件的 SHA-256 全部核验；内置人设与源码一致，不含原作者专属称呼或本机固定路径。JS 语法与 git diff --check 通过。
- 本次使用模拟模型、Apple 和微信，没有新增真实模型 API 调用或验证风格输出的主观质量。

日志：reports/node-0.3.1-full.log、reports/node-0.3.1-personas-final.log、reports/desktop-0.3.1-personas.log；桌面修正后的恢复用例另记 reports/desktop-0.3.1-resume-final.log。

产物：dist/telos-harness-0.3.1-macos-arm64.tgz，SHA-256：`31639ed7c7c72e05cda7a4f9d40d991b389562b0eb472aaf10fd670791274c62`。

---

# 0.3.0 精简验证（2026-10-19）

本轮只修改独立 telos-harness 仓库与测试环境。没有替换用户当前桌面安装、修改默认 DSH_HOME、移动语音模型或切换生产微信机器人；telos-personal-main 工作区仍干净。

## 边界调整

- 分发包的 cordis.patch.yml 只声明 telos-bootstrap；不再覆盖 DSH 全局模型、提示词或工具配置，不再补装凭据、Skill 注册、检查点和语音服务。
- 模型选择来自 DSH agentDefaultModel。Telos 首次配置不接受模型地址或 Key，不写 credentials；旧非敏感模型字段保留但不使用。
- 原生 Skill filesystem / Skill tool / todo 与 Apple、地图工具在 DSH Agent setup 中配置，只属于 Telos 会话。没有新增循环或 Skill 调度器。卸载释放原生会话和本机组件。
- 地图 Token 由地图工具逐次解析，通过已鉴权的本机传输元数据注入；不写模型参数、历史或子进程启动环境。后端凭据回显仍脱敏。
- 语音是可选宿主依赖，不再由 Telos 下载或准备；语音缺失时文字和图片可用，既有 DSH SenseVoice 模型可复用。
- 宿主可以并行调度工具，业务入口按会话串行保护预览，避免两个草稿覆盖待确认内容；确认与操作账本保持原机制。

## 回归结果

- 官方 Node/CLI 完整回归：**215 passed, 42 skipped**，176.41 秒，reports/node-0.3.0-full.log。
- 官方桌面运行时最终完整回归：**217 passed, 40 skipped**，179.50 秒，reports/desktop-0.3.0-final-full.log。
- 后续补充用例：Node 宿主 Skill/归档边界 2 通过、并行预览保护 1 通过；桌面同组 3 通过。归档检查是重跑的已有用例，不能把三个全部算作新增。
- 覆盖首次配置、无语音运行、真实 SenseVoice 对合成 SILK 的识别、图片下载/解密、参数修正、多需求、四项批量预览/确认/保存、原生 steer、重启恢复、重复确认、升级与卸载。
- 桌面额外验证：模型和地图凭据保持不变，拒绝 Telos 页面提交的 Key，独立 DSH 会话没有 Telos 业务工具或提示词，禁用/重启/卸载后宿主可继续使用。

早期桌面测试失败来自测试宿主的辅助标题请求消耗了脚本响应，以及测试把 DSH 自己维护凭据文件的行为误当作 Telos 修改；另外修正了探针导入/服务声明与接口就绪等待。上述问题已修正并通过最终整套回归，未绕过断言。标准基础配置使用压缩历史，因此历史存在检查兼容 jsonl 与 jsonl.zstd。

## 真实模型评测

使用同样的六类虚构多需求输入，每类重复三次，通过实际安装包、模拟微信收发和模拟 Apple 运行：**18/18 通过**，167.87 秒。

同时要求创建预览完整、查询答案进入回复、没有未经确认的写入。仅使用通用业务提示与虚构数据，不加载私人聊天、人设或真实日历；测试隔离 DSH Skill 根目录。有限样本不代表所有真实请求都不会遗漏。

报告：reports/live-package-0.3.0.log 与 reports/live-package-20261019-155643.jsonl。未重新连接真实微信、未在另一台实体 Mac 验证系统授权。

## 归档和升级

产物：dist/telos-harness-0.3.0-macos-arm64.tgz（约 21 MB），同目录附 SHA-256；未发布到 GitHub。

CLI 改用 dsh-base 的 headless profile，旧 sdk-minimal 不再由 Telos 自动补齐服务。升级前须在 DSH 自身配置模型与凭据，保持相同 DSH_HOME 以复用原生历史和业务账本。首次启用语音需由宿主准备；Telos 不包含识别模型。详见 packaging/README.md。

---

# 验证记录

## 0.2.1 桌面与 CLI 兼容（2026-10-19）

验证平台：本机 macOS Apple Silicon；官方 Node/CLI 与官网下载的桌面版均为 DSH `0.2.1-rc.2`。新包尚未发布到 GitHub。

实际在独立 `DSH_HOME` 中通过桌面“插件”页面安装、启用和配置 Telos；模拟微信二维码正常显示，模型凭据持久化后重装仍可复用，启动后完成预览及文字确认保存。最终归档重新安装、启用后显示“Telos 正在运行”，禁用后组件关闭。全过程没有接入生产微信账号，也没有写入真实日历。

新增自动回归使用未经修改的官方 Desktop Host 和归档包，而非替代启动器：覆盖桌面 host/port 参数、空配置启动、认证保护、凭据保留、扫码及授权入口、参数纠错、压缩历史恢复、重复确认、停止后修改配置，以及配置版本错误不退出 Host。测试探针只存在临时 profile，调用原生插件管理器验证禁用、重新启用和卸载，保留已有业务数据库。

本轮还修复了三个实际兼容问题：

- 桌面默认压缩历史为 `.jsonl.zstd`，恢复检测现可识别，历史仍由 Harness 读取。
- 进程和通道通过 Cordis `effect` 注册资源释放，停止或禁用会等待本机组件退出，避免重启抢占旧实例目录。
- 插件插入的组件使用独立标识；桌面复用已有 credentials 和 Skill 服务，避免重复注册及卸载时误判内置组件被占用。

模拟微信服务改为保留消息并按客户端游标确认。断开的旧长轮询不能再删除新进程尚未收到的消息，另有确定性竞态测试覆盖。

最终整套回归记录：Node/CLI 为 211 通过、1 失败、42 跳过；桌面运行时为 213 通过、1 失败、40 跳过。两次失败均来自测试覆盖文件仍引用改名前的语音组件，导致未复用已下载模型而进入下载超时；已修改测试覆盖的组件标识，并单独重跑完整媒体测试：Node/CLI 与桌面运行时均为 6 通过、1 跳过。其余完整流程通过，包括原生 steer、确认账本和插件生命周期。

跳过项目主要为未启用的外部真实模型评测；Node 运行时那组另跳过了需要显式桌面 app 路径的两项测试。媒体回归使用真实本地 SenseVoice 识别，Apple、微信和模型交互使用模拟服务。本轮未额外调用真实模型 API，也未重新验证真实微信账号登录和另一台 Mac 的系统授权。

归档：`telos-harness-0.2.1-macos-arm64.tgz`。SHA-256：`600b64e3728a21242b7754f799947d2d404d5c9fd46d89cd2e6e9a4536d30d34`。

## 0.2.0 发布验证（历史记录）

验证日期：2026-10-19。平台：macOS Apple Silicon；运行时：官方 Node/CLI DSH `0.2.1-rc.2`。

从独立仓库目录执行完整回归：**211 passed, 40 skipped**，159.72 秒。已使用 Release 中的实际安装包；真实模型评测默认关闭，按需准备的服务测试可能跳过，不能把跳过项计作通过。

覆盖范围包括多需求、参数纠错、批量预览、文字确认、部分失败、重启恢复、写入回执、防重复写入、原生 steer、首次配置和升级，以及加密图片和语音的微信处理链路。外部 Apple/微信服务为模拟服务，未接管生产机器人。

本次发布前，同一安装包的真实 DeepSeek 视觉专项测试已通过（1 项）；本次整理独立仓库没有重新调用模型 API。完整真实模型评测脚本保留在 tests/ 中，需显式提供本机配置才能运行。

源码及归档已扫描已配置凭据和常见密钥格式；未发现凭据。归档内 JavaScript 与本仓库插件源码一致，SHA-256 校验通过。

归档：`telos-harness-0.2.0-macos-arm64.tgz`

SHA-256：`710900c8733ecc291ae8b30525f3aa480983fd46215ee8b1d9679f80555b9891`

未验证范围：官网桌面版 DSH、Intel Mac、另一台实体 Mac 的系统授权及 Developer ID 公证。安装包使用 ad-hoc 签名，作为私有预览版发布。


## 0.2.0 官网桌面版初次验证（历史记录，2026-10-19）

从 https://www.deepseek.com/harness/ 的 macOS Apple Silicon 下载入口取得 `0.2.1-rc.2`。镜像中的应用通过 codesign 完整校验及 spctl 公证检查。使用独立 DSH_HOME、Electron 数据目录和模拟微信/Apple 服务验证；未登录真实微信、未写入真实日历、未切换生产机器人。

### 桌面界面

- 在“插件 → 添加插件”输入 tgz 的绝对路径，可成功安装 `telos-harness 0.2.0`。CLI 使用的 `telos-harness@file:...` 写法不适用于该界面的输入框。
- 点击“立即启用”后，后台连接中断。
- 使用未修改的桌面 Host 入口复现，退出码为 2。Host 传入 `--no-open --host 127.0.0.1 --port 0`，插件 `bootstrap.mjs` 只接受单个 `--telos-*` 参数或空参数，因此报错并调用 appExit。
- 当前首次配置仍依赖 CLI 命令，不能宣称已经具备桌面界面的完整首次使用流程。
- 验证后关闭桌面应用，并在隔离 desktop profile 中禁用 Telos bundle，保留已安装的包供后续适配。

### 桌面自带 CLI

直接使用应用内的 `Contents/Resources/runtime/cli/bin/dsh`，无需额外安装 Node/npm。测试启动器使用同一应用的 Electron Node 模式和官方 `dsh-desktop-host/lib/cli.js` 入口，插件归档未改动。

- 首轮安装包专项：4 通过、1 失败。
- 重启与媒体复测：7 通过、1 跳过，包含真实本地 SenseVoice 对合成中文 SILK 的转写；视觉模型 API 测试跳过。
- 完整离线回归：210 通过、1 失败、40 跳过；本轮未重新调用真实模型 API。
- 两次失败均为同一“重启后确认”测试的模拟微信长轮询竞态。记录显示：旧轮询在停止旧进程前开始；旧进程退出后，测试立即推入确认；模拟服务把确认交给该旧连接并从队列删除；新进程的轮询只取得空列表。确认消息没有进入业务层。
- 只在独立诊断脚本中增加“等待模拟服务器的旧轮询结束”，相同归档的首次配置、重启、升级及重复确认连续 3 次通过。没有修改插件，也没有将原始完整回归的失败改记为通过。测试模拟器的该竞态仍需在测试源码中修复。

当时的结论：桌面应用提供的运行时可承载 0.2.0 插件，但该版本桌面界面入口尚不兼容。下一步应适配启动参数和首次配置，保留已有业务工具、确认与防重复写入；不需要替换 Harness 循环。

## 2026-10-19 — 0.4.5 原生用户文件记忆

接入前已将 0.4.4 提交并推送到私有仓库，备份提交 `60ba5e0`。每用户目录由渠道、租户和用户 ID 生成；模型不能选路径。DSH agent-instructions 负责文件检查、去重、更新和恢复。Desktop 独立挂载；CLI 复用已有加载器，用到 user.md 的符号链接接入其候选文件名，避免双加载器基线互相替换。

工具使用 DSH atomic-write 的文件锁和原子写入，完整内容与 expected_content 比较后提交，1,375 Unicode 码点上限，不截断。未新增刷新状态机或压缩策略。

验证：源码回归 238 passed / 42 skipped，另补旧会话工作目录迁移 1 passed；安装包/实际隔离 Desktop/媒体回归 17 passed / 2 skipped / 1 旧工具列表断言失败，修正预期新增 user_memory 后该项复查 1 passed。覆盖多用户和多租户隔离、同用户跨会话共享、外部修改、同内容重写去重、动态时间/pending/receipt 变化、Unicode 字符边界、冲突不覆盖、清空、重启恢复、原生压缩与旧会话迁移。Desktop 安装包测试实际通过微信模拟协议调用 user_memory，退出后重新启动宿主验证资料进入用户消息而非系统提示词。

报告：reports/memory-source.log、reports/memory-regression.log、reports/memory-migration.log、reports/memory-package.log、reports/memory-package-recheck.log。安装包重建 Python 组件，Apple 已签名可执行文件与 0.4.4 哈希一致。
