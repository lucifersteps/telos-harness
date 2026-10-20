# 开发、构建与验证

用户安装请从 [项目首页](../../README.md) 或 [AI 安装指南](../install-for-ai.md) 开始。普通使用者下载 Release 安装包即可，无需构建环境。

## 源码与验证

- `plugin/`：Harness 插件入口、工具注册与原生语音适配。
- `business.py`、`channel.py`、`settings.py`、`sidecar.py`：业务与微信本机组件。
- `checkout/src/`：构建和测试所用的 Telos 业务源码快照。
- `apple_component.py`：通过 macOS LaunchServices 调用独立 Apple 授权组件。
- `personas/`：供微信会话选择的内置人设；可另外选择本机自定义文件。
- `skills/`：随包提供的 Skill 及第三方许可。
- `tests/`：模拟模型、Apple、微信及地图的完整流程回归，以及显式启用的真实模型测试。
- `packaging/`：独立安装包构建脚本和安装说明。

以下路径与命令均相对仓库根目录。使用 Python 3.14.6，安装锁定依赖：

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
.venv/bin/python packaging/build.py --version 0.4.10
```

Apple 权限授予独立的「Telos Apple」，桌面版和 CLI 使用同一路径。本版为 ad-hoc 签名：组件内容更新后可能需要重新授权，本机已验证这一限制。正式分发可通过构建参数 `--sign-identity "Developer ID Application: …"` 使用稳定签名；当前尚未完成 Developer ID 签名、公证及另一台实体 Mac 验证。第三方组件遵循各自许可；此私有仓库未授予 Telos 源码公开再分发许可。

## 发布与文档同步

发布新版本时，上传同一构建产生的 `.tgz` 和 `.tgz.sha256`，注明宿主版本、平台与签名状态；同步首页的开发版/发布版状态，以及 AI 安装指南中的版本。安装包内 README 来自 `packaging/README.md`。不要仅更新源码后就把开发包写成已发布版本。

公开发布还需明确 Telos 的许可条款，并完成公开下载、另一台实体 Mac 的首次安装与授权验证；本次文档调整不改变仓库可见性或许可证。
