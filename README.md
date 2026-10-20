# Telos

把微信变成你的 AI 助手入口：聊天、管理 Apple 日历和提醒事项、记住偏好、查找网页，以及按时发来提醒。Telos 是 [DeepSeek Harness（DSH）](https://www.deepseek.com/en/harness/) 插件，支持 DSH 桌面版和 CLI。

你可以在微信里说：

- “看看我今天的日程和还没完成的提醒事项。”
- “明天下午三点安排一小时项目讨论。”——先展示预览，收到你的文字确认后才写入。
- “记住，我喜欢简短直接的回复。”
- “十分钟后提醒我休息。”——需要 DSH 和 Telos 保持运行。

## 安装前

| 项目 | 要求 |
| --- | --- |
| 电脑 | macOS 14 或更新版本，Apple Silicon（M 系列芯片）；目前不支持 Intel Mac、Windows、Linux |
| 宿主 | 已验证 DSH **0.2.1-rc.2** 的官方桌面版和 Node/CLI |
| 模型 | DSH 中可用的模型连接；可复用已有配置，或登录账号、填写自己的 API Key |
| 微信 | 用户本人扫码登录 |
| Apple 功能 | 首次配置默认开启日历和提醒事项，由用户在 macOS 弹窗中授权 |

安装包自带 Telos 所需的 Python 运行环境和 Apple 组件，无需安装 Python、Homebrew 或 Xcode。选用 Node/CLI 时，仍需要 DSH 对应的 Node.js 运行环境。

**当前版本：0.4.10 预览版。** 从 [0.4.10 Release](https://github.com/lucifersteps/telos-harness/releases/tag/v0.4.10) 下载安装包与 SHA256 校验文件。仓库目前仍为私有，下载需要获授权的 GitHub 账号；普通用户需等待仓库公开或由维护者提供安装包。不要把源码 ZIP 当作插件安装包。

当前预览包使用 ad-hoc 签名，尚未完成 Developer ID 公证及另一台实体 Mac 的首次安装验证；如遇系统拦截，见 [排障指南](docs/troubleshooting.md#macos-blocked)。

## 让 AI 帮你安装

把下面这段话交给能够读取网页、操作本机文件和终端的 AI：

> 请帮我安装和配置 Telos：https://github.com/lucifersteps/telos-harness
>
> 请先阅读 README 和 docs/install-for-ai.md，检查我的系统、已有 DSH 环境和可用的 Release 安装包，按指南安装。优先复用已有模型配置，默认开启 Apple 日历与提醒事项，并引导我完成授权。需要我登录、输入密钥或微信扫码时，请告诉我操作位置，密钥不要放进聊天。最后检查运行状态，并引导我发送一条微信消息、查询一次日历和提醒事项来验证。

**给安装 AI：** 下一步请阅读 [AI 安装指南](docs/install-for-ai.md)，根据实际宿主选择桌面或 CLI 路径；不要直接克隆源码后执行 `npm install` 来替代插件安装。

只有聊天能力的 AI 也能按指南提供操作步骤，但不能代替你在电脑上执行。私有仓库需要访问权限；公开仓库后，普通用户才能直接通过链接读取文档和下载附件。

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
| 百度地图 | 可选，需要自己的百度 Token |
| 语音消息 | 可选，在 DSH 中启用并准备语音识别服务；Telos 不另行下载语音模型 |
| 网页搜索与读取 | 复用 DSH 的网页服务和凭据；模型聊天可用并不代表搜索后端已就绪 |
| 用户记忆、当前会话历史回查 | 由插件接入；历史回查包含压缩前消息，只允许查询当前微信会话 |
| 定时提醒 | 无额外调度 API Key；任务在微信中管理，不显示在桌面普通会话任务列表中 |

密钥直接填入 DSH 的凭据界面，CLI 用户可按安装指南在本机配置。不要把 API Key、微信登录文件或完整聊天日志粘贴到 AI 聊天或 GitHub。

## 怎样算安装成功

- Telos 已启动，模型连接可用。
- 扫码的微信能收到机器人对测试消息的实际回复。
- 日历与提醒事项权限均已允许，两项只读查询成功。若尚未授权，说明“聊天可用，Apple 待授权”，不要将整个安装报告为完成。

需要验证定时任务时，可在微信中创建“一分钟后提醒我安装测试完成”的一次性任务，并保持电脑唤醒、DSH 和 Telos 运行。

## 日常使用、升级与帮助

桌面版重新打开 DSH 后会尝试恢复已登录的 Telos；CLI 需要用相同的数据目录和 profile 重新启动。不要同时运行两个实例处理同一个微信账号。

升级前停止 Telos 并备份数据，安装新包后重启宿主。正常升级保留配置、微信登录、历史和业务记录，无需重新扫码；卸载插件不等于删除这些数据。详见 [升级与恢复](docs/install-for-ai.md#upgrade)。

- [AI 安装指南](docs/install-for-ai.md)：环境检查、下载校验、桌面与 CLI 配置、验收。
- [排障指南](docs/troubleshooting.md)：无法下载、模型错误、扫码、权限、网页和定时任务问题。
- [详细配置与功能说明](packaging/README.md)：人设、语音、记忆、数据位置和业务确认。
- [更新记录](CHANGELOG.md) · [开发与构建](docs/development/README.md) · [验证范围](docs/development/validation.md)。

安装包不附带个人密钥、微信登录状态或私人聊天记录。Telos 的分发许可仍以仓库及安装包中的声明为准，公开访问本身不代表授予开源许可。
