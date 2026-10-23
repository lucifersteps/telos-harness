# Telos AI 安装指南

本指南供能操作用户本机的 AI 执行，也可供用户手动操作。目标是完成微信聊天、默认启用的 Apple 日历与提醒事项配置，并留下可重复的启动方式。只有网页或聊天能力时，请给出对应步骤，由用户执行。

适用版本：Telos **0.4.14**，已验证 DSH **0.2.1-rc.2**，macOS 14+ Apple Silicon。安装其他版本时，应阅读该 Release/tag 对应的文档，不要承诺旧包具有新功能。

## 从仓库链接开始

用户明确要求安装时，先读本指南，再执行环境检查；只有一个链接、意图不清楚时，先确认是安装还是了解项目。无需要求用户手动下载源码。GitHub 页面正文读取失败时，可以读取 [README 原文](https://raw.githubusercontent.com/lucifersteps/telos-harness/main/README.md) 和 [本指南原文](https://raw.githubusercontent.com/lucifersteps/telos-harness/main/docs/install-for-ai.md)。

先确认工具操作的是**用户要运行 Telos 的那台 Mac**。Linux 云端沙箱、远程开发容器或只能聊天的 AI，不能把自己的环境检查当成用户电脑的检查。没有本机访问能力时，给出下一步本机操作并等待结果，不在沙箱中尝试安装 macOS 包。

## 执行约定

- 优先复用已有安装、模型与数据目录。先识别现状，再修改；现有配置只合并必要字段，不整份覆盖。
- 首次配置默认启用 Apple 日历与提醒事项，并引导两项授权。0.4.13 默认直接保存普通单项安排，升级前说明该变化；用户可在插件页关闭“直接写入”，CLI 见 [写入模式](#direct-write)。百度地图和语音可稍后添加。
- 由用户本人完成账号登录、凭据输入、微信扫码及 macOS 授权。提供具体页面或本机文件位置；不要要求用户把密钥发到聊天，不打印凭据文件。
- 环境允许的检查、下载、安装和非敏感配置可连续完成。遇到必须由用户完成的步骤时，说明正在等待什么，收到实际成功状态后继续。
- 不把“插件文件安装成功”“模型已配置”“微信已登录”“已收到回复”混为一谈。失败时保留已完成步骤，参照 [排障指南](troubleshooting.md) 继续。
- 如果安装 AI 本身就在目标 DSH 会话中运行，优先使用宿主的原生插件管理能力。需要退出或重启这个宿主时，先留下剩余步骤、安装包和数据目录，再交给用户或宿主外的工具完成；不要先关闭承载自己的进程而丢失后续安装步骤。

<a id="preflight"></a>
## 1. 检查环境并选择路径

在本机终端检查：

```sh
sw_vers -productVersion
uname -m
command -v dsh
```

需要 macOS 14+ 和 arm64。若 Apple Silicon 上终端显示 `x86_64`，检查是否通过 Rosetta 运行，切换原生终端后再判断；其他平台停止安装当前包。

`command -v dsh` 没有输出不等于没安装桌面版。有输出也可能是桌面自带的命令入口，不能据此认定用户在使用独立 CLI。检查正在运行的 DSH、`/Applications` 和 `~/Applications`；没有找到时询问安装位置，应用也可能被用户放在其他目录。只读取路径与版本，不打印整个进程环境或凭据。

按以下顺序选择宿主：

| 当前情况 | 安装路径 |
| --- | --- |
| 用户明确指定桌面版或 CLI | 遵从选择；已有另一套数据时先说明，不能当成自动迁移 |
| 已有 Telos，未要求切换宿主 | 沿用实际承载它的宿主、profile 和数据目录，走升级流程 |
| 同时存在两种方式，无法判断使用哪套 | 只确认一次实际承载 Telos 的宿主 |
| 已有桌面版，或安装 AI 正在 DSH 桌面中运行 | 复用桌面版 |
| 只有独立 CLI，且用户平时用它运行 DSH | 复用 CLI |
| 完全没有 DSH，用户未指定 | 默认推荐并安装桌面版；从官方 macOS 下载入口开始 |

**不能仅因为 AI 擅长终端命令就自动选择 CLI。** 桌面版有图形配置、扫码与状态页，也自带可供 AI 安装插件的 CLI；这仍是桌面安装。如果 AI 无法操作界面，可由用户完成图形步骤；改为纯 CLI 应由用户选择。

记录以下非敏感信息，后续安装、配置、启动和升级都使用同一组值：

| 信息 | 如何确定 |
| --- | --- |
| 宿主类型和版本 | 桌面应用“关于”或 CLI 的 `dsh --version` |
| 启动入口 | 桌面 `.app` 的实际路径，或 CLI `dsh` 的实际路径 |
| DSH_HOME | 当前实例设置的目录；未覆盖时通常是 `~/.dsh` |
| profile | 桌面用 `desktop`；本指南新建 CLI 用 `telos` |
| TELOS_HOME | 已显式设置则沿用，否则是该 DSH_HOME 下的 `telos` |
| 已有状态 | Telos 版本、是否运行、是否已登录；只检查存在性和非敏感状态 |

选定后只执行 [桌面路径](#desktop) 或 [CLI 路径](#cli) 中的一条。不要为管理桌面 profile 再安装一份 Node CLI，也不要两条流程都执行。

已有 Telos 安装走 [升级与恢复](#upgrade)，不要把它当新安装重新扫码。宿主版本不同于已验证版本时，先查对应 Release 的兼容说明；不要自动降级用户现有 DSH。

<a id="download"></a>
## 2. 获取安装包并校验

安装来源：[Telos Releases](https://github.com/lucifersteps/telos-harness/releases)。普通使用者不需要克隆源码、安装 Python 或执行构建脚本。

本指南对应 [0.4.14 Release](https://github.com/lucifersteps/telos-harness/releases/tag/v0.4.14)，附件包含安装包与 SHA256 校验文件。以下是该版已发布的固定附件地址，不依赖 GitHub API、GitHub 账号、`gh`、Git 或 Python。没有现成下载时，在用户 Mac 的终端执行：

```sh
mkdir -p "$HOME/Downloads"
TELOS_DOWNLOAD_DIR="$(mktemp -d "$HOME/Downloads/telos-0.4.14.XXXXXX")"
cd "$TELOS_DOWNLOAD_DIR" || exit 1
curl --fail --location --retry 2 --connect-timeout 20 \
  --output telos-harness-0.4.14-macos-arm64.tgz \
  https://github.com/lucifersteps/telos-harness/releases/download/v0.4.14/telos-harness-0.4.14-macos-arm64.tgz || exit 1
curl --fail --location --retry 2 --connect-timeout 20 \
  --output telos-harness-0.4.14-macos-arm64.tgz.sha256 \
  https://github.com/lucifersteps/telos-harness/releases/download/v0.4.14/telos-harness-0.4.14-macos-arm64.tgz.sha256 || exit 1
shasum -a 256 -c telos-harness-0.4.14-macos-arm64.tgz.sha256 || exit 1
TELOS_ARCHIVE="$TELOS_DOWNLOAD_DIR/telos-harness-0.4.14-macos-arm64.tgz"
```

每步失败即停止，不安装部分下载的文件。换到新终端时，需要重新设置 `TELOS_ARCHIVE` 为已下载文件的绝对路径。也可让用户从 Release 页面下载这两个同名附件，再在它们所在目录校验。

已配置 GitHub CLI 的环境也可用下面这条替代路径，不必重复下载。没有 `gh`、未登录或匿名 GitHub API 被限流（可能返回 403）时，使用上面的直接下载方式，不为公开下载索要令牌。下载仍失败时报告实际网络或 HTTP 错误，不自动降级、猜新版本地址或改用第三方镜像。

```sh
gh release list --repo lucifersteps/telos-harness --limit 10
gh release view v0.4.14 --repo lucifersteps/telos-harness --json tagName,assets,url
```

确认版本存在后，在新建的空下载目录执行：

```sh
gh release download v0.4.14 --repo lucifersteps/telos-harness \
  --pattern 'telos-harness-0.4.14-macos-arm64.tgz' \
  --pattern 'telos-harness-0.4.14-macos-arm64.tgz.sha256'
shasum -a 256 -c telos-harness-0.4.14-macos-arm64.tgz.sha256
```

预期结果为文件名后显示 `OK`。失败则停止安装并重新下载。没有 `.sha256` 附件时，可计算 `shasum -a 256 安装包路径` 并与该 Release 附件的 GitHub `digest` 或维护者提供的 SHA256 比较；仅计算本地哈希不算完成校验。两者都没有时，先取得校验值。

检查包内元数据，核对版本、平台与宿主要求：

```sh
tar -xOf telos-harness-0.4.14-macos-arm64.tgz package/package.json
tar -xOf telos-harness-0.4.14-macos-arm64.tgz package/component-manifest.json
```

应是 `telos-harness`、目标版本、`darwin` / `arm64`，manifest 的 `min_macos` 为 `14`、`harness` 为 `0.2.1rc2`。安装变量使用下载文件的**绝对路径**，不要把示例路径原样执行：

```sh
TELOS_ARCHIVE="/absolute/path/telos-harness-0.4.14-macos-arm64.tgz"
test -f "$TELOS_ARCHIVE"
```

<a id="desktop"></a>
## 3A. 桌面版安装与配置

### 安装插件

尚未安装 DSH 时，从 [DSH 官方页面](https://www.deepseek.com/en/harness/) 选择 **Download Desktop → macOS (Apple silicon)**，下载官方 DMG。打开后将 `DeepSeek Harness.app` 安装到用户选定的固定位置（通常是 `/Applications` 或 `~/Applications`），不要长期从挂载的 DMG 或临时目录运行。桌面版自带运行环境，不必先装 Node.js/npm。

首次打开应用，由用户处理系统提示及登录，确认能进入主界面，并记录“关于”中的版本和实际 `.app` 路径；首次打开也让宿主建立自己的 desktop profile。官网链接指向最新版本，不保证永远是 0.2.1-rc.2；版本不同先核对兼容说明，不覆盖已有安装或假定完全兼容。系统阻拦时按 [macOS 排障](troubleshooting.md#macos-blocked) 处理。

首选图形入口：打开 DSH → 插件 → 添加插件 → 选择已校验的 `.tgz` → 启用 Telos。

AI 需要通过终端安装时，使用**该桌面应用自带的 CLI**。先确定实际 `.app` 路径、桌面实例的 DSH_HOME，退出 DSH，再执行：

```sh
TELOS_DSH_APP="/Applications/DeepSeek Harness.app"
TELOS_DESKTOP_HOME="$HOME/.dsh"
test -x "$TELOS_DSH_APP/Contents/Resources/runtime/cli/bin/dsh"
env DSH_HOME="$TELOS_DESKTOP_HOME" \
  "$TELOS_DSH_APP/Contents/Resources/runtime/cli/bin/dsh" \
  plugin --profile desktop add "telos-harness@file:$TELOS_ARCHIVE" --offline
open "$TELOS_DSH_APP"
```

上面两个路径都是示例，执行前替换为检查得到的值。若桌面使用自定义 DSH_HOME，应沿用原来的启动方式；普通 `open` 不保证继承终端里的环境变量。`--offline` 表示从已下载归档安装，不代表后续模型、微信、网页功能可以离线使用。

确认插件列表显示预期版本且 Telos 页面可打开。不要对 `desktop` profile 执行 `--telos-setup/login/authorize`；桌面配置使用 Telos 页面。

### 模型、微信与 Apple

1. **选择模型。** 已有可用配置时选“跟随 DSH 默认模型”或已有 API 模型。没有配置时，通过 Telos 页的 DeepSeek 账号入口登录，再刷新并选择可用的账号模型；登录成功不代表原先选中的 API 模型已改用账号。也可展开“添加其他 API 模型”，填写协议、地址和模型 ID。凭据由用户直接在界面中输入并“保存到 DSH”，需要 DeepSeek API Key 时见下方 [获取模型凭据](#model-key)。
2. **测试连接。** 点击“测试连接”，成功后继续。该测试发送一条简短模型请求，会产生少量费用；不读取 Apple 数据。账号模型可用不代表 `DEEPSEEK_API_KEY` 或网页搜索后端也已就绪。
3. **保存选项。** 默认助手即可。保留 Apple 开关开启；说明“直接写入”默认开启，可按用户偏好关闭；没有百度 Token 时关闭地图。点击“保存并应用”。Telos 现有程序默认两类工具均开启，因此应明确保存地图选择。
4. **微信扫码。** 实例未运行时点击扫码登录，用户用自己的微信扫描页面二维码并确认。等待登录成功；二维码过期则重新发起。不要将二维码作为长期凭据保存。
5. **Apple 授权。** 点击“授权日历与提醒事项”，用户在 macOS 弹窗中分别允许“Telos Apple”访问日历与提醒事项。检查两项状态；若拒绝，按 [权限排障](troubleshooting.md#apple) 补授权。
6. **启动。** 点击“启动 Telos”，等待页面显示运行中，再执行 [验收](#verify)。

暂时不授权 Apple 时，可以启动并验证聊天，但应标注 Apple 配置尚未完成。运行中需要重新扫码或重新发起授权时，先点击“停止 Telos”；操作完成后再启动。

<a id="cli"></a>
## 3B. CLI 安装与配置

### 准备 DSH

仅在前面已选定 CLI 时执行。复用已有兼容的官方 Node CLI；未安装时，从 [Node.js 官网](https://nodejs.org/) 安装与宿主兼容的 Node.js。核对时的 [DSH 0.2.1-rc.2 源码声明](https://github.com/deepseek-ai/deepseek-harness/blob/master/package.json) 要求 `^22.19.0 || >=24.0.0`；新安装优先选择满足范围的受支持 LTS，后续版本重新核对。npm 发布包的 `engines` 可能为空，不能据此断言任意 Node 版本都可用。

```sh
node --version
npm view @deepseek-ai/dsh@0.2.1-rc.2 engines --json
npm install -g @deepseek-ai/dsh@0.2.1-rc.2
dsh --version
```

若已有其他版本，不直接覆盖或降级；先确定兼容性或选择独立安装位置。权限错误时处理 Node 安装目录，不默认使用 `sudo`。官方项目入口见 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)。

### 确定数据目录并创建 profile

已有 Telos 时沿用它的 DSH_HOME、TELOS_HOME 和 profile。只有全新 CLI 安装才采用下面的独立目录：

```sh
export DSH_HOME="$HOME/Library/Application Support/Telos Harness"
mkdir -p "$DSH_HOME/profiles/telos"
```

后续所有 CLI 命令都在保留此变量的同一个终端中执行。新开终端必须重新设置；不要把桌面已有账号和凭据自动复制到新目录。检查是否已有 `TELOS_HOME` 环境变量，若存在，先确定那是不是预期的业务目录。

仅在 `$DSH_HOME/profiles/telos/package.json` **不存在**时创建以下内容：

```json
{"name":"dsh-profile-telos","private":true,"dsh":{"profile":{"bundles":["@deepseek-ai/dsh-base"]}}}
```

已有文件时检查 `bundles` 是否包含 `@deepseek-ai/dsh-base`，保留其他字段。该 profile 无需 Web、TUI 或 SDK JSON-RPC 入口，旧 `sdk-minimal` 不能直接承载本版 Telos。

安装已校验的插件包：

```sh
dsh plugin --profile telos add "telos-harness@file:$TELOS_ARCHIVE" --offline
dsh --profile telos --telos-setup
```

`--telos-setup` 只保存 Telos 非敏感配置，不配置模型 Key，也不自动完成登录或授权。

### 配置模型与凭据

同一 DSH_HOME/profile 已有可用模型时沿用原配置。全新 base profile 的默认模型是 `deepseek-official / deepseek-flash`，需要可用的 DeepSeek API 凭据；不要假设其他目录的桌面账号登录会自动共享。

<a id="model-key"></a>
需要新 Key 时，由用户打开 [DeepSeek 开放平台](https://platform.deepseek.com/)，在 API keys 页面创建并检查账户的 API 服务状态；官方步骤见 [首次 API 调用](https://api-docs.deepseek.com/)。网页聊天账号可用不等于 API 已有额度。已在 DSH 中使用可用账号模型或其他供应商时，继续复用，不强制再申请 DeepSeek API Key。

凭据优先使用 DSH 原生管理方式。纯 CLI 用户也可**亲自在本机编辑** `$DSH_HOME/.env`，添加 `DEEPSEEK_API_KEY`。下面只打开文件，不读取或显示其内容：

```sh
umask 077
touch "$DSH_HOME/.env"
chmod 600 "$DSH_HOME/.env"
open -t "$DSH_HOME/.env"
```

让用户在编辑器填写 `DEEPSEEK_API_KEY=自己的密钥` 并保存。已有文件只修改对应条目，不覆盖其他配置；不要将真实值写进命令行、聊天或日志。无需百度地图时不必填写百度 Token。

需要显式选择这个默认模型时，在 `$DSH_HOME/profiles/telos/cordis.patch.yml` 的 YAML 列表中合并以下条目；同一 `id` 已存在时更新该条目：

```yaml
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-flash
```

保持现有其他条目及所需配置字段。其他供应商应按 DSH 原生配置方式添加，再选择其准确的 provider/model；Telos 没有 `--telos-api-key`、`--telos-model` 或 `--telos-configure` 命令，不要编造参数。

### Apple 默认开启，地图按需配置

停止该 Telos 实例后，编辑业务目录中的 `config.json`，**合并**以下字段：

```json
{
  "tools": {"apple": true, "maps": false},
  "model_selection": null
}
```

这不是完整配置文件，不能整份替换。保留 `version`、`wechat`、人设及其他现有字段。新安装的业务目录默认是 `$DSH_HOME/telos`；有 `TELOS_HOME` 时使用其实际目录。`model_selection: null` 表示跟随宿主；已有独立模型选择时保留它，不用示例覆盖。保存后文件权限设为 `600`。

这里默认开启日历与提醒事项，关闭尚未配置 Token 的地图。用户需要地图时，按 [百度地图配置](#baidu) 获取 Agent Plan SK，保存到同一 DSH 凭据环境，并将 `maps` 改为 `true`。本流程不修改软件默认值，只保存用户本次安装的选项。

### 登录、授权、检查与启动

逐步执行，每步成功后再继续：

```sh
dsh --profile telos --telos-login
dsh --profile telos --telos-authorize
dsh --profile telos --telos-status
dsh --profile telos
```

- `--telos-login` 在终端展示二维码。单独启动这一步并保持进程运行，由用户扫码确认，等待 `wechat_login: true`；不要放进会立刻超时或关闭的批量命令。AI 终端无法完整展示二维码时，让用户在本机终端执行这一步，不跳过登录。
- `--telos-authorize` 打开 macOS 授权流程，用户允许“Telos Apple”的日历与提醒事项权限。
- `--telos-status` 输出配置、登录、Apple、模型选择和语音状态。`configuration`、`wechat_login`、`wechat_enabled` 应为 `true`，Apple 两项应显示已授权。**它不会实际测试模型，也没有“通道健康”结论。**
- 最后一个命令会持续运行，按 `Ctrl+C` 停止。看到进程存在还不算验收成功，继续下面的微信验证。

给用户保留日常启动命令；使用自定义目录或其他 profile 时换成实际值：

```sh
env DSH_HOME="$HOME/Library/Application Support/Telos Harness" dsh --profile telos
```

有独立 TELOS_HOME 时也要保留它。终端由 AI 临时工具托管时，确认结束 AI 会话后进程是否仍存活；不能保证时，让用户在自己的终端执行启动命令。Telos 当前不安装开机自启或 launchd 服务。

<a id="verify"></a>
## 4. 验收并交付使用方式

让用户在刚扫码的微信中完成以下检查；AI 已被用户明确授权代发测试消息时，也可以代为操作：

| 检查 | 操作 | 成功依据 |
| --- | --- | --- |
| 微信与模型 | “你好，请回复收到” | 微信实际收到模型回复，不只是进程启动或扫码成功 |
| 日历读取 | “查看今天的日程” | Apple 查询成功；空日历也可以，但不能是权限或工具错误 |
| 提醒事项读取 | “查看未完成的提醒事项” | 查询成功；没有事项也可以 |
| 可选定时任务 | “一分钟后提醒我安装测试完成” | 工具返回创建成功，并在保持在线时收到到期消息 |

验收默认只读，不为了测试修改用户的真实安排。用户愿意验证直接写入时，让用户在微信明确要求新增一条唯一命名的临时事项：应收到一张完整保存卡片，再回复 `1` 撤销并确认临时事项已清理。关闭直接写入时应先预览，回复 `1` 确认；不能把这两种卡片的数字含义混淆。

交付时用简短清单说明：安装版本、桌面/CLI 入口、数据目录、如何启动/停止、已通过的检查、仍待用户完成的项。Apple 尚未授权时明确写“聊天已就绪，Apple 待授权”；未测的网页、语音或定时能力不要写成“全部通过”。定时任务要求电脑唤醒且 DSH/Telos 运行。

<a id="baidu"></a>
## 可选：获取和配置百度地图 Token

Telos 调用百度地图 **Agent Plan** 接口，需要 **SK（Service Key）**。桌面界面把它称为“百度地图 Token”，DSH 中的凭据名称是 `BAIDU_MAP_AUTH_TOKEN`。

### 去哪里获取

1. 打开 [百度地图 Agent Plan 官方页面](https://lbs.baidu.com/apiconsole/agentplan)。
2. 由用户本人登录百度账号，按页面提示获取 Agent Plan 的 SK。若当前页面要求开通服务或选择套餐，先让用户了解额度与费用，再由用户决定。
3. 用户自行复制 SK，并直接填入下面的本机配置位置。AI 不需要读取、回显或代为保管它。

百度的 [官方 Agent Plan 说明](https://github.com/baidu-maps/workbuddy-agent-plugin/blob/main/skills/baidu-ai-map/SKILL.md) 区分了 SK 和其他地图产品的 AK。不要引导用户到普通 AK 管理页创建服务器端或浏览器端 AK，也不需要为 Telos 另装百度 MCP 服务。具体按钮名称、额度和价格以登录后的官方页面为准；不要承诺固定免费额度。

### 桌面版填在哪里

打开 Telos 页面 → 开启“百度地图” → 在“百度地图 Token”输入框粘贴 SK → 点击“保存到 DSH” → 点击“保存并应用”。

“已配置”只表示凭据已保存，不代表接口鉴权或额度已验证。凭据不填进人设、Skill 文件或聊天。

### CLI 填在哪里

在当前实例的 DSH 凭据环境中保存；使用 `.env` 时，由用户在本机编辑当前 `$DSH_HOME/.env`，添加或更新这一项：

```dotenv
BAIDU_MAP_AUTH_TOKEN=用户自己的AgentPlanSK
```

上面是格式示例，不是可用密钥。保留其他条目，文件权限设为 `600`，不要输出文件内容。填写原始 SK 即可，不加 `Bearer ` 前缀；鉴权前缀由 Telos 自动添加。

停止当前 CLI 实例，在实际业务目录的 `config.json` 中将 `tools.maps` 设为 `true`，保留 `tools.apple: true` 和其他配置；然后用原 DSH_HOME、TELOS_HOME、profile 重启。

### 怎样验证成功

在微信发送“请用百度地图查一下北京颐和园的地址”。工具实际返回地点信息才算通过；“Token 已保存”或模型凭常识回答都不算。

如果返回认证、额度或网络错误，按 [地图排障](troubleshooting.md#optional) 检查。测试可能消耗百度额度，使用一个明确地点的一次查询即可。暂时不配置时保持地图关闭，微信聊天和已授权的 Apple 功能仍可使用。

<a id="upgrade"></a>
## 5. 升级、恢复与中断后继续

1. 记录原版本、DSH_HOME、TELOS_HOME、profile 和启动方式；停止 Telos。桌面替换包前退出应用，CLI 在承载实例的终端停止进程。
2. 在用户本机备份 DSH_HOME；TELOS_HOME 在外部时另行备份。备份包含凭据和聊天资料，不上传到仓库或公开日志。
3. 下载并校验新包，用同一个宿主和 profile 执行安装；不要重建已有 profile、覆盖 `.env` 或清空业务目录。
4. 重新打开桌面应用或执行原 CLI 启动命令。仅更新安装文件不足以替换仍在运行的旧进程。
5. 核对版本、登录、两项权限和微信回复。通常无需重新扫码；Apple 组件签名变化后可能需要重新授权。

配置中断时，从尚未完成的一步继续：已有正确安装包就不重复下载，已登录就不重新扫码，已授权就查询状态。正在运行时先停止再修改配置。不要同时启动桌面和 CLI 来轮询同一微信账号。

发生版本不兼容时保留现场和备份，参照对应版本说明恢复；不要保证任意旧版能读取新版数据。卸载插件与删除数据是两件事，除非用户明确要求，不删除历史、凭据、微信登录状态或业务账本。
<a id="cli-office"></a>
## CLI 的 Office 文件读取环境

Telos 0.4.12 起支持文件读取。桌面版优先复用 DSH 已有的 `load_workspace_dependencies`，不重复安装 Python 库。

独立 CLI 使用相同的官方 Office 技能。已有官方 primary-runtime 时，启动前设置 `DSH_PRIMARY_RUNTIME` 为包含 `runtime.json` 的绝对目录。若机器尚无此环境，可使用官方 Python runtime 包的准备接口（需要网络和磁盘空间；由安装 AI 先解释并按用户授权操作）：

```sh
python3 -m venv "$HOME/.dsh-office-bootstrap"
"$HOME/.dsh-office-bootstrap/bin/python" -m pip install 'deepseek-harness-runtime-bin==0.2.1rc2'
export DSH_PRIMARY_RUNTIME="$("$HOME/.dsh-office-bootstrap/bin/python" -c 'from deepseek_harness_runtime import download_primary_runtime; print(download_primary_runtime())')"
dsh --profile telos
```

沿用现有的 DSH_HOME、TELOS_HOME 和 profile。首次准备完成后，保存返回的绝对路径到该 CLI 的启动环境，后续启动只需设置此路径；不要每次聊天重新下载安装。可以使用现有专用 Python 环境运行官方准备接口，已有目录不应覆盖重建。下载失败时排查网络并使用官方接口重试，不拼装运行环境、不跳过校验。

Telos 只把该路径交给 DSH 原生依赖工具；环境校验、路径返回和 Office 技能由 DSH 实现。若用户选择自己的 Python 环境，则按官方技能使用已安装的 python-docx/openpyxl，不在微信会话中自行安装包。PDF 文字层使用系统 PDFKit，不依赖这套 Office 环境；扫描 PDF 不包含 OCR 保证。


<a id="direct-write"></a>
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

0.4.14 会同时保留保存卡片和本轮独立问题的答案。例如“把健身移到明天相同时间，明天有什么安排？”应收到完整修改卡片及日程查询答案；两者分别发送，回复【1】仍对应这次保存。升级验收可用临时事项加独立查询验证，最后撤销临时事项。

保存后收到一张完整卡片，底部提示“回复【1】撤销；需要修改时直接回复修改内容”。回复 `1` 撤销当前已投递的保存卡片；待确认预览中的 `1` 仍表示确认。也可发送 `撤销` 撤回最近一次直接保存。当前微信使用文字指令，没有新增可点击的撤销按钮。撤销新增会删除该次新建的对象，撤销修改会恢复所改字段。对象后来被修改/删除时拒绝自动覆盖；重复撤销不会继续回退上一条；最新回复已不是保存卡片时，`1` 不会寻找更早的卡片。已触发通知无法收回，复杂操作不承诺自动回滚。取消尚未保存的预览仍发送 `取消`，已保存对象需用撤销或明确提出修改。

原值与保存结果记录在既有本地操作账本中，不另建任务数据库。不要清空数据目录；撤销依赖保存记录。遇到未知写入/未知撤销结果时先查询核实，不能反复创建或自行重放。

### 0.4.13 的 Apple 扩展能力

提醒事项支持标签读取、添加、替换和清空，以及分区、子任务、图片/网页附件、紧急状态和序号星期重复；提醒事项只支持一个提前提醒。日历支持新建、改名、改色和删除日历容器，以及日程出行时间和会议参与者、忙闲状态等只读信息。日历容器与其中的单条日程是不同对象。复杂操作继续预览确认，标签等可恢复的普通单项修改可直接保存。

示例：“给提交报销加上工作标签”“把准备材料移到本周分区”“为明天的会议设置 30 分钟出行时间”。这些扩展使用 Apple 原生接口，已在 macOS 26.2 验证；其他 macOS 版本的扩展行为仍需验证。出现不支持或读取失败时保留错误，不通过访问 Apple 内部数据库兜底。
