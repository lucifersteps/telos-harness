# Telos Harness 插件（隔离预览版）

安装在 DeepSeek Harness 0.2.1rc2 的独立 `sdk-minimal` profile。当前包支持 macOS 14+ Apple Silicon；Intel 尚未构建验证。
组件已包含 Python 运行环境和编译后的 Apple helper，使用者无需安装 Python、Homebrew 或 Xcode。

```sh
export DSH_HOME="$HOME/Library/Application Support/Telos Harness"
dsh plugin --profile sdk-minimal add telos-harness@file:/path/to/telos-harness-0.2.0-macos-arm64.tgz --offline
dsh --profile sdk-minimal --telos-setup
dsh --profile sdk-minimal --telos-login
dsh --profile sdk-minimal --telos-authorize
dsh --profile sdk-minimal --telos-status
dsh --profile sdk-minimal --telos-prepare-voice
dsh --profile sdk-minimal
```

`--telos-setup` 隐藏输入 API Key，调用 Harness 原生 credentials 服务持久化；不会打印 Key。
已有 Harness 凭据可直接复用；百度 Token 可留空，路线查询在配置后可用。
`--telos-login` 显示微信二维码，仅允许登录账号本人私聊，禁用群聊。
`--telos-authorize` 请求系统日历和提醒事项授权，必须由设备使用者完成。
不含原作者的模型 Key、地图 Token、微信登录状态、人设或个人数据。

数据默认放在 `$DSH_HOME/telos`；可用 `TELOS_HOME` 改变业务数据位置。Harness 历史仍由 `$DSH_HOME` 管理。
`config.json` 是非敏感配置；凭据由 Harness 管理，微信 Token 在权限受限的业务目录中。
进程退出时插件关闭本机组件；同一数据目录只允许一个实例，不抢占或终止已有实例。
关闭整个 Telos bundle 时微信轮询及 Apple/地图工具一起关闭。

升级前停止该隔离实例，备份整个 `DSH_HOME`（包含凭据、历史、草稿和账本），再用相同命令安装新归档。
升级只替换插件文件；不重新扫码、不覆盖配置、不移动数据。新版本不支持配置版本时拒绝启动。
回退同样安装上一版本；若未来版本更改存储格式，应恢复对应备份，不能只降级程序。
后台常驻由用户自己的进程管理器启动上述 Harness 命令；插件不安装 launchd 服务、不自动接管其他机器人。

模型循环、历史、todo、Skill 均由原生 Harness 提供。微信新消息只通过 steer 输入。
Apple 工具只生成预览；实际写入必须通过用户文字确认、送达及版本校验和操作账本。
图片使用原生附件；本隔离版仍遵循 Harness 的附件存储行为，尚不能替代旧机器人的媒体保留策略。
语音使用 DSH 官方 `speech-to-text` 服务及 `speech-to-text-sensevoice` 后端，首次执行 `--telos-prepare-voice` 下载校验后的 SenseVoice INT8 和 VAD 模型（约 241 MB）。准备完成后可离线识别，重启复用原生模型目录；`--telos-status` 查看准备状态。不会启用 Web/TUI、麦克风录音、语音输入界面或多 Agent。
Telos 只下载、解密微信媒体，并通过内置 SILK 编解码器和 macOS `afconvert` 转为 16 kHz、单声道、PCM16 WAV。识别及工作进程由 DSH 管理，不需要 Whisper、ffmpeg 或另设转写缓存。单段音频上限 120 秒、下载上限 16 MiB；失败保留同条消息的文字补充，不推测音频内容。
语音内容以带来源标记的文字进入原生 steer；确认保存仍需要文字确认。Telos 临时音频和图片处理后删除；图片进入 DSH 后的附件和会话保留仍遵循 Harness 原生行为。

语音需要官方完整 Node 发行版 `@deepseek-ai/dsh@0.2.1-rc.2`。本次检查的 Python 所带单文件版缺少语音资源，`--telos-prepare-voice` 会报告资源缺失；不能据此认为语音已经可用。使用 Node.js 22.19+ 的官方安装方式 `npm install -g @deepseek-ai/dsh@0.2.1-rc.2`，然后以同一个 `DSH_HOME` 启动即可。无需启用 Web/TUI。
安装包采用本机 ad-hoc 签名，尚未做 Developer ID 签名、公证和另一台实体 Mac 的授权验证。
