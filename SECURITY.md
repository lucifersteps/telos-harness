# 安全问题报告

当前维护版本为 0.4.11。旧版本建议升级。

请通过 GitHub 的 [私密漏洞报告入口](https://github.com/lucifersteps/telos-harness/security/advisories/new) 向维护者报告安全问题，不要先在公开 Issue 或 PR 中披露可利用细节。

请包含受影响版本、桌面版或 CLI、必要的复现步骤、预期与实际结果。使用虚构账号和数据；不要发送 API Key、微信凭据、二维码、聊天记录或完整数据目录。如果入口不可用，可先创建不包含漏洞细节的 Issue，请维护者提供私密沟通方式。

Telos 在你的 Mac 上处理微信消息，并可调用 DeepSeek Harness 的模型、网页、历史检索与调度服务。Apple 提醒事项和日历权限由 macOS 管理。不要把自己的配置目录、日志或数据库上传至公开仓库。

本项目没有固定的安全响应时限。安装包目前使用 ad-hoc 签名，尚未完成 Apple Developer ID 签名、公证或另一台实体 Mac 验证；请从项目 Releases 获取安装包并核对 SHA256。
