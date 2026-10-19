# Telos Harness

为 DeepSeek Harness 提供微信收发、Apple 日历与提醒事项、百度地图及 Skill 的插件。Harness 管理循环、历史、上下文压缩和任务清单；Telos 管理业务工具、用户确认及防重复写入。

当前开发版本 **0.4.7（预览版）**，支持 macOS 14+ Apple Silicon。兼容官方 Node/CLI 和官网桌面版 DSH `0.2.1-rc.2`，两种入口使用同一个插件包、业务工具与确认账本。插件只安装 Telos 入口；DSH 统一提供模型、凭据、Skill、历史和可选语音。CLI 改用包含 dsh-base 的独立 profile。验证范围见 [验证记录](docs/development/validation.md)。

## 安装

构建后取得 `dist/telos-harness-0.4.7-macos-arm64.tgz` 和校验文件（本次开发包尚未发布到 GitHub），在包所在目录执行：

```sh
shasum -a 256 -c telos-harness-0.4.7-macos-arm64.tgz.sha256
```

随后按 [安装和首次配置说明](packaging/README.md) 安装。安装包包含 Python 运行环境与 Apple helper，使用者无需安装 Python、Homebrew 或 Xcode。仍需配置自己的模型凭据、扫码连接微信、授予 Apple 权限；地图功能需要百度 Token。语音由 DSH 配置和准备，Telos 不下载模型。

仓库及安装包包含可选的通用黎曼人设，不含个人密钥、微信登录状态、个人资料或聊天记录。安装不会自动切换其他正在运行的机器人。

微信会话只注入当前待确认内容、保存回执和消息时间；Apple 数据由现有 query/detail 工具按需读取。同一消息的未变化上下文由 DSH 原生机制去重。桌面宿主将压缩组件交给 Agent 预设，Telos 直接创建的微信会话会在独立作用域挂载 DSH 原生压缩与工具结果修剪组件；CLI 已有压缩服务时沿用宿主配置。

通道不生成固定“暂停/中断/已结束”回复，只发送实际模型回答和业务回执。CLI 更新归档后须重启 DSH，使业务进程和已加载模块采用新版本。

## 开发与验证

- `plugin/`：Harness 插件入口、工具注册与原生语音适配。
- `business.py`、`channel.py`、`settings.py`、`sidecar.py`：业务与微信本机组件。
- `checkout/src/`：构建和测试所用的 Telos 业务源码快照。
- `apple_component.py`：通过 macOS LaunchServices 调用独立 Apple 授权组件。
- `personas/`：供微信会话选择的内置人设；可另外选择本机自定义文件。
- `skills/`：随包提供的 Skill 及第三方许可。
- `tests/`：模拟模型、Apple、微信及地图的完整流程回归，以及显式启用的真实模型测试。
- `packaging/`：独立安装包构建脚本和安装说明。

使用 Python 3.14.6，安装锁定依赖：

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm install -g @deepseek-ai/dsh@0.2.1-rc.2
export TELOS_TEST_NODE="$(command -v node)"
export TELOS_TEST_DSH="$(npm root -g)/@deepseek-ai/dsh/lib/bin.js"
bash run-validation.sh
```

将 Release 安装包放入 `dist/` 后，回归包含实际安装、首次配置、重启和升级；缺少安装包时相关测试跳过。设置 `TELOS_TEST_DESKTOP_APP` 为官方 `.app` 路径，可追加真实 Desktop Host 的配置、扫码、重启确认回归。另设 `TELOS_APPLE_PERMISSIONS_LIVE=1` 可运行真实系统授权、重启和升级测试，需本人处理 macOS 弹窗；测试只读取 Apple 数据，不写入或连接微信。设置 `TELOS_TEST_SPEECH_ROOT` 为准备好的 SenseVoice 数据目录可额外执行真实本地语音测试。默认不调用外部模型 API。

真实模型评测须显式设置 `TELOS_NATIVE_MODEL_HOME`，指向本机配置目录（`config/personal.json` 的 `providers` 配置和 `.env`），再执行 `bash run-validation.sh live-native`、`live-channel` 或 `live-package`。请使用自己的凭据；测试使用虚构输入和模拟 Apple/微信，不写入真实日历。日志留在被 Git 忽略的 `reports/`。

构建需要 Xcode Command Line Tools 和上述 Python 依赖：

```sh
.venv/bin/python packaging/build.py --version 0.4.7
```

Apple 权限授予独立的「Telos Apple」，桌面版和 CLI 使用同一路径。本版为 ad-hoc 签名：组件内容更新后可能需要重新授权，本机已验证这一限制。正式分发可通过构建参数 `--sign-identity "Developer ID Application: …"` 使用稳定签名；当前尚未完成 Developer ID 签名、公证及另一台实体 Mac 验证。第三方组件遵循各自许可；此私有仓库未授予 Telos 源码公开再分发许可。
