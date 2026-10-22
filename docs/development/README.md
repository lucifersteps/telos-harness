# 开发、构建与验证

用户安装请从 [项目首页](../../README.md) 或 [AI 安装指南](../install-for-ai.md) 开始。普通使用者下载 Release 安装包即可，无需构建环境。

## 源码与验证

- `plugin/`：Harness 插件入口、工具注册与原生语音适配。
- `business.py`、`channel.py`、`settings.py`、`sidecar.py`：业务与微信本机组件。
- `checkout/src/`：构建和测试所用的 Telos 业务源码快照。
- `apple_component.py`：通过 macOS LaunchServices 调用独立 Apple 授权组件。
- `documents.py`：仅保存微信附件原件；`plugin/documents.mjs` 组合 DSH 原生工具与 Office 技能。`plugin/document-skills/telos-pdf/` 是系统 PDFKit 的小型调用指南与适配脚本，不是 PDF 解析器。
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
.venv/bin/python packaging/build.py --version 0.4.12
```

Apple 权限授予独立的「Telos Apple」，桌面版和 CLI 使用同一路径。本版为 ad-hoc 签名：组件内容更新后可能需要重新授权，本机已验证这一限制。正式分发可通过构建参数 `--sign-identity "Developer ID Application: …"` 使用稳定签名；当前尚未完成 Developer ID 签名、公证及另一台实体 Mac 验证。Telos 原创代码采用 MIT；第三方组件遵循各自许可，见根目录 LICENSE、THIRD_PARTY_NOTICES.md 与安装包 licenses/。

## 发布与文档同步

文件读取回归使用 `tests/test_documents.py`。设置 `TELOS_TEST_ARCHIVE` 指向待验证的安装包、`TELOS_TEST_DESKTOP_APP` 指向官方桌面应用；CLI 可另外设置 `TELOS_TEST_PRIMARY_RUNTIME` 指向官方 primary-runtime，未指定时测试可复用该桌面应用的 runtime 资源。Word/Excel 测试依赖官方运行环境，不依赖 Telos 自带解析库。`tests/fixtures/documents/` 仅包含虚构会议、空白页与测试密码 PDF，不含用户资料。部署 CLI 的环境准备见 [安装指南](../install-for-ai.md#cli-office)。

发布新版本时，上传同一构建产生的 `.tgz` 和 `.tgz.sha256`，注明宿主版本、平台与签名状态；同步首页的开发版/发布版状态，以及 AI 安装指南中的版本。安装包内 README 来自 `packaging/README.md`。不要仅更新源码后就把开发包写成已发布版本。

发布前检查 Git 历史和最终包中的凭据与个人数据，运行合并影响范围的回归，校验包内许可证、清单和 SHA256。另一台实体 Mac 的首次安装与授权尚未验证，发布说明必须保留这一限制。

标签开发回归：`tests/business_tests/test_tags.py` 覆盖字段、预览和冲突；`tests/test_tags.py` 使用实际 CLI/Desktop Host 与模拟服务验证确认和重启。`TELOS_TAGS_LIVE=1` 显式启用 `tests/test_tags_live.py`，使用刚编译并获用户授权的 Apple 组件，只创建、修改、清理唯一命名的临时测试清单与事项。默认不会写入真实 Apple 数据。可用 `TELOS_TAGS_HELPER` 指定测试组件，不能指向不含标签能力的旧版本。

Apple 扩展开发版的验证入口：`tests/business_tests/test_extended_apple.py`（参数/预览/筛选）、`tests/test_extended_apple_live.py`（显式 `TELOS_EXTENDED_LIVE=1`，独立已授权测试组件和临时数据）、`tests/test_extended_apple_hosts.py`（桌面/CLI 宿主）。具体范围和版本记录见 [validation.md](validation.md)。

直接写入回归：`tests/test_direct_write.py` 覆盖策略、幂等、撤销冲突、中断恢复与回执重投；`tests/test_direct_write_hosts.py` 使用隔离 CLI/Desktop Host 验证开关、重启和旧确认流程；`TELOS_DIRECT_LIVE=1` 显式启用 `tests/test_direct_write_live.py`，使用独立已授权 Apple 测试组件和临时数据，不操作现有安排。
