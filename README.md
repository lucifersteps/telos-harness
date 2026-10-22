# Telos

**事务交给 Telos 打理，忙碌也能有条不紊。**

你是否也有过这样的经历：工作和生活中的安排一多，总担心遗漏。为了把事情安排得井井有条，不断下载、尝试各类日程和清单 App，却总要逐项输入标题、设置时间和优先级。微信里刚收到一个安排，又得复制到另一个应用；临时改了时间，还要找出原来的记录重新编辑。

本来只是想记住几件事，最后却多了一项工作：维护自己的任务管理系统。认真整理了几天，又因为操作太琐碎，慢慢放弃。

**有了 Telos，你可以直接在微信里，把工作和生活中的安排发给助手。**

想到什么就说什么，也可以发送截图或语音，不必先整理成标准格式。让 AI 帮你梳理事务、补齐信息，再按你的确认保存到 Apple 日历和提醒事项。新增安排、调整时间、查看待办，用日常聊天的方式就能完成，减少在微信和管理 App 之间反复切换。

手机上无需再装一个任务管理 App，也不用重新搭建一套复杂的管理体系。你继续使用熟悉的微信，安排仍保存在 Apple 日历和提醒事项里。Telos 作为 [DeepSeek Harness](https://www.deepseek.com/en/harness/) 插件运行在 Mac 上，帮你完成中间那些繁琐的操作。

![Telos 六格漫画：从遗漏安排、反复尝试管理 App，到在微信里发送截图和语音，由 Telos 整理到 Apple 日历与提醒事项，事务虽多依然从容有序。](docs/assets/telos-story.png)

**多一份从容，从 Telos 开始。** [下载安装包](https://github.com/lucifersteps/telos-harness/releases/tag/v0.4.12)，或按下方的[「让 AI 帮你安装」](#让-ai-帮你安装)开始配置。

[查看六宫格漫画原图](docs/assets/telos-story.png) · [查看宣传海报（含项目二维码）](docs/assets/telos-poster.png)

**使用前提**

Telos 面向使用 **iPhone 和 Mac** 的用户，目前支持 **macOS 14+、Apple Silicon（M 系列芯片）**，需要在 Mac 上安装并运行 DeepSeek Harness 与 Telos。

如果希望随时通过微信使用助手、按时收到定时消息，建议让 Mac 全天保持联网和唤醒，并持续运行 DeepSeek Harness 与 Telos。

## Telos 能做什么？

| 功能 | 支持的操作 | 你可以这样说 |
| --- | --- | --- |
| 📅 Apple 日历 | 查询、创建、修改和删除日程 | “看看我明天有哪些安排。”<br>“明天下午三点安排一小时项目讨论。” |
| ✅ Apple 提醒事项 | 查询、新增、修改、完成和删除待办 | “周五下午五点前提醒我提交报销材料，保存到 Apple 提醒事项。”<br>“把‘买牛奶’标记为已完成。” |
| ⏰ 微信定时任务 | 定时发微信，按计划执行查询；支持查看、修改和取消任务 | “十分钟后在微信提醒我休息。”<br>“每天早上九点，把当天日程和未完成事项发给我。” |
| 🌐 网页搜索与阅读 | 搜索资料、读取公开网页、整理内容 | “查一下这个项目的官方安装文档。”<br>“读一下这个链接，整理主要结论。” |
| 🗺️ 百度地图 | 查询地点、地址、公交与驾车路线、天气 | “从北京南站到颐和园，坐地铁怎么走？”<br>“查询杭州明天的天气。” |
| 🧠 用户记忆 | 保存和更新称呼、偏好等长期信息 | “记住，我喜欢简短直接的回复。”<br>“忘记我之前告诉你的称呼偏好。” |
| 💬 历史回查 | 检索当前微信会话的旧消息，包括压缩前内容及前后文 | “找一下我们之前讨论报销的那段对话。”<br>“查一下我上次提到的会议时间。” |
| 🖼️ 图片与语音 | 理解图片、识别语音，整理为回答或操作草稿 | 发送海报：“提取活动时间和地点，整理成日程草稿。”<br>发送语音：“把这些事情整理成待办。” |
| 📄 文件读取 | 按需读取 PDF 文字层、Word（DOCX）、Excel（XLSX）及文本文件 | 发送文件：“把文件中的会议整理成日程草稿。”<br>“总结这份材料的重点。” |
| 🎭 人设与 Skill | 选择内置或自定义人设，使用 DeepSeek Harness 中适用的 Skill | 在设置中选择“默认助手”或“黎曼”，也可加载自己的提示词文件。 |

**使用说明：** 涉及日历和提醒事项的创建、修改或删除时，Telos 会先展示预览，收到你的文字确认后再执行。地图需要百度 Agent Plan SK，网页需要可用的 DeepSeek Harness 网页服务，图片和语音分别依赖模型视觉能力与语音识别服务。历史回查仅限当前微信会话。

**文件读取：** 支持在微信发送 PDF、Word（DOCX）、Excel（XLSX）和文本文件。复用 DeepSeek Harness 的 Office 技能与工具；PDF 借助 macOS PDFKit 读取文字层，无需另配解析 API Key。桌面版复用内置环境，独立 CLI 需准备 Office 运行环境。格式限制与数据保存方式见[文件读取说明](packaging/README.md#微信文件读取)。

## 安装前

DeepSeek Harness（下文简称 DSH）支持桌面版和 CLI，两种方式使用同一个 Telos 安装包。

| 项目 | 要求 |
| --- | --- |
| 电脑 | macOS 14 或更新版本，Apple Silicon（M 系列芯片）；目前不支持 Intel Mac、Windows、Linux |
| 宿主 | 已验证 DSH **0.2.1-rc.2** 的官方桌面版和 Node/CLI |
| 模型 | DSH 中可用的模型连接；可复用已有配置，或登录账号、填写自己的 API Key |
| 微信 | 用户本人扫码登录 |
| Apple 功能 | 首次配置默认开启日历和提醒事项，由用户在 macOS 弹窗中授权 |

安装包自带 Telos 所需的 Python 运行环境和 Apple 组件，无需安装 Python、Homebrew 或 Xcode。选用 Node/CLI 时，仍需要 DSH 对应的 Node.js 运行环境。

**当前版本：0.4.12。** 从 [0.4.12 Release](https://github.com/lucifersteps/telos-harness/releases/tag/v0.4.12) 下载安装包与 SHA256 校验文件。不要把源码 ZIP 当作插件安装包。

当前安装包使用 ad-hoc 签名，尚未完成 Developer ID 公证及另一台实体 Mac 的首次安装验证；如遇系统拦截，见 [排障指南](docs/troubleshooting.md#macos-blocked)。

**插件市场安装来源：** 使用 [0.4.12 预构建安装包](https://github.com/lucifersteps/telos-harness/releases/download/v0.4.12/telos-harness-0.4.12-macos-arm64.tgz)，并核对同一 Release 的 SHA256 文件。仓库根目录的 `package.json` 声明正式插件元数据；源码不包含已编译的 Python 和 Apple 组件，不能直接用 `github:lucifersteps/telos-harness` 代替安装包。市场安装完成后，仍需完成模型配置、微信扫码和 Apple 授权。

## 让 AI 帮你安装

最简方式：把 **“帮我安装 Telos” + 本仓库链接** 交给能够操作这台 Mac 的 AI。只发链接时，有些 AI 只会介绍项目；只有云端沙箱或聊天能力的 AI 也无法直接安装到你的电脑。

**默认选择：已有 DSH 就复用；完全没装过时优先安装桌面版。** 只有你明确选择命令行，或原来就在使用 CLI，才走 CLI 安装。AI 使用终端给桌面版安装插件，不代表把你改成了 CLI 用户。

也可以复制这段更完整的说明：

> 请帮我安装和配置 Telos：https://github.com/lucifersteps/telos-harness
>
> 请先阅读 README 和 docs/install-for-ai.md，在我的 Mac 上检查环境并安装。复用已有 DSH；没有时优先安装桌面版，不要仅因为方便执行命令就改装 CLI。复用已有模型配置，默认开启 Apple 日历与提醒事项。需要我登录、输入密钥或微信扫码时，请告诉我操作位置，密钥不要放进聊天。最后检查运行状态，并引导我发送一条微信消息、查询一次日历和提醒事项来验证。

**给安装 AI：** 下一步请阅读 [AI 安装指南](docs/install-for-ai.md)，根据实际宿主选择桌面或 CLI 路径；不要直接克隆源码后执行 `npm install` 来替代插件安装。

无需 GitHub 账号或 GitHub CLI 即可读取文档和下载附件；[AI 安装指南](docs/install-for-ai.md#download) 提供直接下载命令。账号登录、密钥输入、微信扫码和 macOS 授权仍由你本人完成。

## 自己安装

### DSH 桌面版（推荐首次使用）

1. 从 [DSH 官网](https://www.deepseek.com/en/harness/) 安装兼容版本，打开应用。
2. 从 [Telos Releases](https://github.com/lucifersteps/telos-harness/releases) 下载与你所用文档版本匹配的 `telos-harness-版本-macos-arm64.tgz`，按 [下载与校验](docs/install-for-ai.md#download) 核对文件。
3. 在 DSH 的“插件 → 添加插件”安装该文件并启用，打开 Telos 页面。
4. 选择已有模型或跟随 DSH 默认模型；也可以使用页面中的账号登录或 API 模型配置。“测试连接”成功后，保留 Apple 开关开启，百度地图按需开启，点击“保存并应用”。
5. 在 Telos 页面完成微信扫码，点击“授权日历与提醒事项”，在 macOS 弹窗中分别允许两项权限。
6. 点击“启动 Telos”。用扫码的微信向机器人发送“你好，请回复收到”，然后试试“查看今天的日程和未完成提醒事项”。

如果暂时不授予 Apple 权限，可以先使用微信聊天，之后再补授权。模型连接测试和聊天会产生所选模型的使用费用。

详细步骤及 AI 可用的桌面安装命令见 [桌面安装](docs/install-for-ai.md#desktop)。

### DSH CLI

按照 [CLI 安装步骤](docs/install-for-ai.md#cli) 创建或复用包含 `dsh-base` 的 profile，配置模型凭据，再依次执行首次配置、微信登录、Apple 授权和启动。

CLI 与桌面使用同一个 `.tgz` 安装包。**CLI 必须保持运行**；关闭运行它的终端、退出 DSH 或电脑休眠时，不能按正常在线状态接收消息和触发定时任务。

## 哪些功能需要额外准备

| 功能 | 配置方式 |
| --- | --- |
| 微信聊天 | DSH 模型连接、微信扫码；已有模型可直接复用，不一定要新增 API Key |
| Apple 日历与提醒事项 | 默认开启，两项 macOS 权限均需本人授权；写入前仍需文字确认 |
| 百度地图 | 可选，在 [百度地图 Agent Plan](https://lbs.baidu.com/apiconsole/agentplan) 登录并获取 SK，填入 Telos 的“百度地图 Token” |
| 语音消息 | 可选，在 DSH 中启用并准备语音识别服务；Telos 不另行下载语音模型 |
| 网页搜索与读取 | 复用 DSH 的网页服务和凭据；模型聊天可用并不代表搜索后端已就绪 |
| 用户记忆、当前会话历史回查 | 由插件接入；历史回查包含压缩前消息，只允许查询当前微信会话 |
| 定时提醒 | 无额外调度 API Key；任务在微信中管理，不显示在桌面普通会话任务列表中 |

密钥直接填入 DSH 的凭据界面，CLI 用户可按安装指南在本机配置。不要把 API Key、微信登录文件或完整聊天日志粘贴到 AI 聊天或 GitHub。

### 百度地图 Token 去哪里获取

打开 [百度地图 Agent Plan 官方页面](https://lbs.baidu.com/apiconsole/agentplan)，登录百度账号，按页面指引获取 **SK（Service Key）**。Telos 的“百度地图 Token”和配置项 `BAIDU_MAP_AUTH_TOKEN` 都填写这个 SK；普通地图 Web API、JavaScript API 或 MCP 使用的 AK 不适用于这里。

桌面版在 Telos 页开启“百度地图”，把 SK 填入“百度地图 Token”，点击“保存到 DSH”，再“保存并应用”。CLI 用户将它保存在当前 DSH 的凭据环境中，并开启地图工具。申请、配置和测试步骤见 [百度地图配置指南](docs/install-for-ai.md#baidu)。

额度和收费以百度当前页面为准；暂时不使用地图可以跳过，不影响微信聊天及已授权的 Apple 功能。

## 怎样算安装成功

- Telos 已启动，模型连接可用。
- 扫码的微信能收到机器人对测试消息的实际回复。
- 日历与提醒事项权限均已允许，两项只读查询成功。若尚未授权，说明“聊天可用，Apple 待授权”，不要将整个安装报告为完成。

需要验证定时任务时，可在微信中创建“一分钟后提醒我安装测试完成”的一次性任务，并保持电脑唤醒、DSH 和 Telos 运行。

开发版已加入可关闭的“直接写入”模式：普通单项安排直接保存，可在微信回复“撤销”；删除、批量等操作仍需确认。新老用户升级该开发版后默认开启，详见 [写入模式说明](docs/install-for-ai.md#direct-write)。此功能尚未包含在正式 0.4.12 安装包中。

## 日常使用、升级与帮助

桌面版重新打开 DSH 后会尝试恢复已登录的 Telos；CLI 需要用相同的数据目录和 profile 重新启动。不要同时运行两个实例处理同一个微信账号。

升级前停止 Telos 并备份数据，安装新包后重启宿主。正常升级保留配置、微信登录、历史和业务记录，无需重新扫码；卸载插件不等于删除这些数据。详见 [升级与恢复](docs/install-for-ai.md#upgrade)。

- [AI 安装指南](docs/install-for-ai.md)：环境检查、下载校验、桌面与 CLI 配置、验收。
- [排障指南](docs/troubleshooting.md)：无法下载、模型错误、扫码、权限、网页和定时任务问题。
- [详细配置与功能说明](packaging/README.md)：人设、语音、记忆、数据位置和业务确认。
- [更新记录](CHANGELOG.md) · [开发与构建](docs/development/README.md) · [验证范围](docs/development/validation.md)。
- [参与贡献](CONTRIBUTING.md) · [贡献者](CONTRIBUTORS.md) · [私密报告安全问题](SECURITY.md)。

## 联系作者

对 Telos 感兴趣，或想交流使用体验、功能建议，欢迎添加作者微信：**`lylsysu`**（请备注「Telos」）。

安装包不附带个人密钥、微信登录状态或私人聊天记录。Telos 原创代码采用 [MIT 许可证](LICENSE)，第三方组件保留各自许可，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。
