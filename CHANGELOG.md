# 更新记录

## [Unreleased]

## [0.2.0] - 2026-10-19

### 新增
- 独立 Telos Harness 插件仓库，包含插件源码、业务组件、测试和 macOS Apple Silicon 打包脚本。
- 首次配置、微信扫码、Apple 授权、状态查询、重启及升级验证。
- 百度地图工具及原生 Skill，Apple 日历和提醒事项工具。

### 变更
- Harness 接管通用循环、历史、任务清单与 Skill 加载，微信通过原生 steer 输入。
- 图片使用 Harness 原生附件；语音使用原生 speech-to-text / SenseVoice，Telos 负责媒体下载、解密及格式转换。
- 保留文字确认、预览版本和送达检查、操作账本及防重复写入；关闭 Web/TUI 和多 Agent。
- 开发测试路径改为可配置，不依赖原作者目录或生产机器人配置。

### 发布范围
- 预览版：macOS 14+ Apple Silicon；验证环境为官方 Node/CLI DSH 0.2.1-rc.2。
- 官网桌面版、Intel、Developer ID 公证及第二台实体 Mac 尚未验证。
