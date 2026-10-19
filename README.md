# Telos Harness

为 DeepSeek Harness 提供微信收发、Apple 日历与提醒事项、百度地图及 Skill 的插件。Harness 管理循环、历史和任务清单；Telos 管理业务工具、用户确认及防重复写入。

当前版本 **0.2.0（预览版）**，支持 macOS 14+ Apple Silicon。已验证官方 Node/CLI `@deepseek-ai/dsh@0.2.1-rc.2`；DeepSeek 官网桌面版的插件安装与运行尚未验证。

## 安装

从本仓库 Releases 下载 `telos-harness-0.2.0-macos-arm64.tgz` 和校验文件，在下载目录执行：

```sh
shasum -a 256 -c telos-harness-0.2.0-macos-arm64.tgz.sha256
```

随后按 [安装和首次配置说明](packaging/README.md) 安装。安装包包含 Python 运行环境与 Apple helper，使用者无需安装 Python、Homebrew 或 Xcode。仍需配置自己的模型凭据、扫码连接微信、授予 Apple 权限；地图功能需要百度 Token。语音首次使用需下载原生 SenseVoice 模型。

仓库及安装包不含个人密钥、微信登录状态、人设或聊天记录。安装不会自动切换其他正在运行的机器人。

## 开发与验证

- `plugin/`：Harness 插件入口、工具注册与原生语音适配。
- `business.py`、`channel.py`、`settings.py`、`sidecar.py`：业务与微信本机组件。
- `checkout/src/`：构建和测试所用的 Telos 业务源码快照。
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

将 Release 安装包放入 `dist/` 后，回归包含实际安装、首次配置、重启和升级；缺少安装包时相关测试跳过。设置 `TELOS_TEST_SPEECH_ROOT` 为准备好的 SenseVoice 数据目录可额外执行真实本地语音测试。默认不调用外部模型 API。

真实模型评测须显式设置 `TELOS_NATIVE_MODEL_HOME`，指向本机配置目录（`config/personal.json` 的 `providers` 配置和 `.env`），再执行 `bash run-validation.sh live-native`、`live-channel` 或 `live-package`。请使用自己的凭据；测试使用虚构输入和模拟 Apple/微信，不写入真实日历。日志留在被 Git 忽略的 `reports/`。

构建需要 Xcode Command Line Tools 和上述 Python 依赖：

```sh
.venv/bin/python packaging/build.py --version 0.2.0
```

本版为 ad-hoc 签名，尚未进行 Developer ID 签名、公证及另一台实体 Mac 的授权验证。第三方组件遵循各自许可；此私有仓库未授予 Telos 源码公开再分发许可。
