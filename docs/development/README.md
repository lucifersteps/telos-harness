# 开发、构建与验证

用户安装请从 [项目首页](../../README.md) 或 [AI 安装指南](../install-for-ai.md) 开始。普通使用者下载 Release 安装包即可，无需构建环境。

## 源码与验证

- `plugin/`：Harness 插件入口、工具注册与原生语音适配。
- `business.py`、`channel.py`、`settings.py`、`sidecar.py`：业务与微信本机组件。
- `checkout/src/`：构建和测试所用的 Telos 业务源码快照。
- `apple_component.py`：通过 macOS LaunchServices 调用独立 Apple 授权组件。
- `plugin/host-tools.mjs`：复用宿主通用工具并限制微信渠道暂未适配的能力。
- `documents.py`：仅保存微信附件原件；`plugin/documents.mjs` 补充原生 Office 技能和文件处理说明。`plugin/document-skills/telos-pdf/` 是系统 PDFKit 的小型调用指南与适配脚本，不是 PDF 解析器。
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
.venv/bin/python packaging/build.py --version 0.4.15
```

Apple 权限授予独立的「Telos Apple」，桌面版和 CLI 使用同一路径。本版为 ad-hoc 签名：组件内容更新后可能需要重新授权，本机已验证这一限制。正式分发可通过构建参数 `--sign-identity "Developer ID Application: …"` 使用稳定签名；当前尚未完成 Developer ID 签名、公证及另一台实体 Mac 验证。Telos 原创代码采用 MIT；第三方组件遵循各自许可，见根目录 LICENSE、THIRD_PARTY_NOTICES.md 与安装包 licenses/。

## 通用工具复用

微信 Agent 在原生 `setup()` 中，通过 `agentPresets.mount(scope, 'standard')` 加入桌面宿主现有的标准预设；没有预设服务的 CLI 直接继承全局工具。Telos 不复制预设 YAML，也不再逐个注册文件、终端、网页、任务清单和技能调用工具。新增宿主通用工具会自动可见，宿主禁用的工具不会由 Telos 擅自重新开启。桌面使用标准预设，不跟随界面为其他会话选择的预设；标准预设失效时明确报错，不静默换成另一套工具。

目前可复用原生文件读写/编辑、图片读取、glob/grep、终端及后台作业查询、网页搜索/读取、技能和任务清单。微信侧继续单独提供 Apple/地图、用户记忆、当前会话历史、微信定时消息；同名历史和调度工具在当前 Agent 范围内覆盖宿主版本。Telos 人设覆盖宿主编程人设，保留原生工具说明。普通 DSH 会话不因此获得 Telos 工具。

以下属于渠道或会话生命周期能力，本阶段不开放：桌面问答框、计划审批、展示面板、跨会话检索，以及子 Agent/工作流和自主目标。它们需要额外的微信交互或异步结果投递适配，不能仅暴露工具名称。微信用户工作目录保持固定，以保留用户记忆的加载范围。微信提问继续使用 `ask_user`；定时任务继续走当前微信会话绑定的原生调度服务。原生后台作业由 `job_output` 等工具查询；完成通知沿用已有定时消息的微信回执投递和去重机制，作为非用户输入处理，不能批准 Apple 写入。文件结果目前不支持附件回传。

执行仍经过 DSH 的原生权限与沙箱，不自动批准权限提升。宿主要求交互授权时，需要在宿主处理，微信回复不替代宿主授权。工具继承不是安全沙箱：文件和终端可访问的范围由宿主权限决定；当前会话历史工具的限制也不等于操作系统级文件隔离。不要将此模式作为对不可信微信用户开放整台 Mac 的安全方案。

`tests/test_host_tools.py` 使用隔离桌面/CLI 宿主，验证宿主新增工具自动继承、原生文件实际读写/编辑/搜索、宿主拒绝策略、禁用工具调用失败、当前会话限制和重启。既有历史压缩、用户记忆、直接写入/撤销、调度、文件技能测试继续覆盖各自边界。测试无需修改用户现用安装。

## 发布与文档同步

微信回复分泡由 `checkout/src/telos/adapters/delivery.py` 统一处理：普通聊天按完整想法分泡，列表标题、列表项目及缩进内容作为一条消息，代码块和业务预览保留完整换行；定时投递复用相同逻辑。单条超长时仍按微信长度限制分段。`tests/business_tests/test_delivery.py` 与 `test_wechat_delivery.py` 覆盖分泡及发送，`tests/test_list_bubbles_hosts.py` 验证安装包在官方 CLI/Desktop 中的实际投递与重启。

文件读取回归使用 `tests/test_documents.py`。设置 `TELOS_TEST_ARCHIVE` 指向待验证的安装包、`TELOS_TEST_DESKTOP_APP` 指向官方桌面应用；CLI 可另外设置 `TELOS_TEST_PRIMARY_RUNTIME` 指向官方 primary-runtime，未指定时测试可复用该桌面应用的 runtime 资源。Word/Excel 测试依赖官方运行环境，不依赖 Telos 自带解析库。`tests/fixtures/documents/` 仅包含虚构会议、空白页与测试密码 PDF，不含用户资料。部署 CLI 的环境准备见 [安装指南](../install-for-ai.md#cli-office)。

发布新版本时，上传同一构建产生的 `.tgz` 和 `.tgz.sha256`，注明宿主版本、平台与签名状态；同步首页的开发版/发布版状态，以及 AI 安装指南中的版本。安装包内 README 来自 `packaging/README.md`。不要仅更新源码后就把开发包写成已发布版本。

发布前检查 Git 历史和最终包中的凭据与个人数据，运行合并影响范围的回归，校验包内许可证、清单和 SHA256。另一台实体 Mac 的首次安装与授权尚未验证，发布说明必须保留这一限制。

标签开发回归：`tests/business_tests/test_tags.py` 覆盖字段、预览和冲突；`tests/test_tags.py` 使用实际 CLI/Desktop Host 与模拟服务验证确认和重启。`TELOS_TAGS_LIVE=1` 显式启用 `tests/test_tags_live.py`，使用刚编译并获用户授权的 Apple 组件，只创建、修改、清理唯一命名的临时测试清单与事项。默认不会写入真实 Apple 数据。可用 `TELOS_TAGS_HELPER` 指定测试组件，不能指向不含标签能力的旧版本。

Apple 扩展能力的验证入口：`tests/business_tests/test_extended_apple.py`（参数/预览/筛选）、`tests/test_extended_apple_live.py`（显式 `TELOS_EXTENDED_LIVE=1`，独立已授权测试组件和临时数据）、`tests/test_extended_apple_hosts.py`（桌面/CLI 宿主）。具体范围和版本记录见 [validation.md](validation.md)。

直接写入回归：`tests/test_direct_write.py` 覆盖策略、幂等、撤销冲突、中断恢复与回执重投；`tests/test_direct_write_hosts.py` 使用隔离 CLI/Desktop Host 验证开关、重启和旧确认流程；`TELOS_DIRECT_LIVE=1` 显式启用 `tests/test_direct_write_live.py`，使用独立已授权 Apple 测试组件和临时数据，不操作现有安排。
