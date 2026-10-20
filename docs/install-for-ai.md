# Telos AI 安装指南

本指南供能操作用户本机的 AI 执行，也可供用户手动操作。目标是完成微信聊天、默认启用的 Apple 日历与提醒事项配置，并留下可重复的启动方式。只有网页或聊天能力时，请给出对应步骤，由用户执行。

适用版本：Telos **0.4.11**，已验证 DSH **0.2.1-rc.2**，macOS 14+ Apple Silicon。安装其他版本时，应阅读该 Release/tag 对应的文档，不要承诺旧包具有新功能。

## 执行约定

- 优先复用已有安装、模型与数据目录。先识别现状，再修改；现有配置只合并必要字段，不整份覆盖。
- 首次配置默认启用 Apple 日历与提醒事项，并引导两项授权。百度地图和语音可稍后添加。
- 由用户本人完成账号登录、凭据输入、微信扫码及 macOS 授权。提供具体页面或本机文件位置；不要要求用户把密钥发到聊天，不打印凭据文件。
- 环境允许的检查、下载、安装和非敏感配置可连续完成。遇到必须由用户完成的步骤时，说明正在等待什么，收到实际成功状态后继续。
- 不把“插件文件安装成功”“模型已配置”“微信已登录”“已收到回复”混为一谈。失败时保留已完成步骤，参照 [排障指南](troubleshooting.md) 继续。

<a id="preflight"></a>
## 1. 检查环境并选择路径

在本机终端检查：

```sh
sw_vers -productVersion
uname -m
command -v dsh
```

需要 macOS 14+ 和 arm64。若 Apple Silicon 上终端显示 `x86_64`，检查是否通过 Rosetta 运行，切换原生终端后再判断；其他平台停止安装当前包。

记录以下非敏感信息，后续安装、配置、启动和升级都使用同一组值：

| 信息 | 如何确定 |
| --- | --- |
| 宿主类型和版本 | 桌面应用“关于”或 CLI 的 `dsh --version` |
| 启动入口 | 桌面 `.app` 的实际路径，或 CLI `dsh` 的实际路径 |
| DSH_HOME | 当前实例设置的目录；未覆盖时通常是 `~/.dsh` |
| profile | 桌面用 `desktop`；本指南新建 CLI 用 `telos` |
| TELOS_HOME | 已显式设置则沿用，否则是该 DSH_HOME 下的 `telos` |
| 已有状态 | Telos 版本、是否运行、是否已登录；只检查存在性和非敏感状态 |

已安装桌面版时优先走 [桌面路径](#desktop)，不要为安装 Telos 另装一份 Node CLI 来管理桌面 profile。用户已有 CLI 运行方式时走 [CLI 路径](#cli)。同时存在两种宿主时，让用户选择实际承载机器人的一个。

已有 Telos 安装走 [升级与恢复](#upgrade)，不要把它当新安装重新扫码。宿主版本不同于已验证版本时，先查对应 Release 的兼容说明；不要自动降级用户现有 DSH。

<a id="download"></a>
## 2. 获取安装包并校验

安装来源：[Telos Releases](https://github.com/lucifersteps/telos-harness/releases)。普通使用者不需要克隆源码、安装 Python 或执行构建脚本。

本指南对应 [0.4.11 Release](https://github.com/lucifersteps/telos-harness/releases/tag/v0.4.11)，附件包含安装包与 SHA256 校验文件。公开附件可直接通过浏览器下载，无需 GitHub 账号。下方 gh 命令供已配置 GitHub CLI 的环境使用；未配置时，从 Release 页面下载同名文件即可，不要为下载公开附件索要用户的令牌。若 Release 或匹配附件不可访问，明确报告原因，不要猜下载链接或自动降级。

有 GitHub CLI 的环境可以先查询；没有 `gh` 时使用浏览器查看 Release 附件即可，不必为了安装 Telos 强制安装 `gh`。

```sh
gh release list --repo lucifersteps/telos-harness --limit 10
gh release view v0.4.11 --repo lucifersteps/telos-harness --json tagName,assets,url
```

确认版本存在后，在新建的空下载目录执行：

```sh
gh release download v0.4.11 --repo lucifersteps/telos-harness \
  --pattern 'telos-harness-0.4.11-macos-arm64.tgz' \
  --pattern 'telos-harness-0.4.11-macos-arm64.tgz.sha256'
shasum -a 256 -c telos-harness-0.4.11-macos-arm64.tgz.sha256
```

预期结果为文件名后显示 `OK`。失败则停止安装并重新下载。没有 `.sha256` 附件时，可计算 `shasum -a 256 安装包路径` 并与该 Release 附件的 GitHub `digest` 或维护者提供的 SHA256 比较；仅计算本地哈希不算完成校验。两者都没有时，先取得校验值。

检查包内元数据，核对版本、平台与宿主要求：

```sh
tar -xOf telos-harness-0.4.11-macos-arm64.tgz package/package.json
tar -xOf telos-harness-0.4.11-macos-arm64.tgz package/component-manifest.json
```

应是 `telos-harness`、目标版本、`darwin` / `arm64`，manifest 的 `min_macos` 为 `14`、`harness` 为 `0.2.1rc2`。安装变量使用下载文件的**绝对路径**，不要把示例路径原样执行：

```sh
TELOS_ARCHIVE="/absolute/path/telos-harness-0.4.11-macos-arm64.tgz"
test -f "$TELOS_ARCHIVE"
```

<a id="desktop"></a>
## 3A. 桌面版安装与配置

### 安装插件

尚未安装 DSH 时，从 [DSH 官方页面](https://www.deepseek.com/en/harness/) 获取兼容的 macOS 版本。若官网只提供其他版本，先核对兼容说明，不假设它与已验证版本行为一致。

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

1. **选择模型。** 已有可用配置时选“跟随 DSH 默认模型”或已有 API 模型。没有配置时，通过 Telos 页的 DeepSeek 账号入口登录，或展开“添加其他 API 模型”，填写协议、地址和模型 ID。凭据由用户直接在界面中输入并“保存到 DSH”。
2. **测试连接。** 点击“测试连接”，成功后继续。该测试发送一条简短模型请求，会产生少量费用；不读取 Apple 数据。账号模型可用不代表 `DEEPSEEK_API_KEY` 或网页搜索后端也已就绪。
3. **保存选项。** 默认助手即可。保留 Apple 开关开启；没有百度 Token 时关闭地图。点击“保存并应用”。Telos 现有程序默认两类工具均开启，因此应明确保存地图选择。
4. **微信扫码。** 实例未运行时点击扫码登录，用户用自己的微信扫描页面二维码并确认。等待登录成功；二维码过期则重新发起。不要将二维码作为长期凭据保存。
5. **Apple 授权。** 点击“授权日历与提醒事项”，用户在 macOS 弹窗中分别允许“Telos Apple”访问日历与提醒事项。检查两项状态；若拒绝，按 [权限排障](troubleshooting.md#apple) 补授权。
6. **启动。** 点击“启动 Telos”，等待页面显示运行中，再执行 [验收](#verify)。

暂时不授权 Apple 时，可以启动并验证聊天，但应标注 Apple 配置尚未完成。运行中需要重新扫码或重新发起授权时，先点击“停止 Telos”；操作完成后再启动。

<a id="cli"></a>
## 3B. CLI 安装与配置

### 准备 DSH

复用已有兼容的官方 Node CLI。未安装时，先从 [Node.js 官网](https://nodejs.org/) 安装满足该 DSH 包 `engines` 要求的 Node.js，再检查并安装指定版本：

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

- `--telos-login` 在终端展示二维码。保持命令运行，由用户扫码确认，等待 `wechat_login: true`。AI 终端无法完整展示二维码时，让用户在本机终端执行这一步，不跳过登录。
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

验收默认只读，不为了测试写入真实日历。用户要验证创建/修改时，先展示具体预览，再等待用户文字确认。

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
