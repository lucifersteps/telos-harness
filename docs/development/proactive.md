# 基础主动消息

## 范围

main 只提供基础心跳、固定问候和后续检查。DSH 持有定时器、会话、模型循环与任务存储；Telos 持有微信收件人校验、检查状态和投递回执。用户定时任务与托管检查共用 DSH 调度，但生命周期独立。

目标执行督促、导师提示词、目标/行动查询约束、ActivityWatch、屏幕时间、健康分析、独立观察气泡和可插拔策略保存在 experimental 分支，main 不注册这些工具或启动健康接收端。

## 配置

`proactive.enabled` 默认 false，开启必须有接收会话。`timezone` 使用 IANA 时区。`heartbeat` 包含 enabled、interval_minutes（1–1440）、start、end、prompt；允许跨午夜，开始结束相同表示全天。`notification_mode` 默认 on_demand，助手有合适内容时联系，否则静默；always 要求每次心跳通知。固定提醒独立指定时间、日期和 prompt，不受心跳通知方式影响。

桌面设置页可修改基础配置、暂停/恢复、查看和取消检查。用户主动指定的“每天九点发日程”仍由原生定时任务工具管理。所有后台检查只允许读取、notify_user 和 schedule_next_check；不能授权 Apple 写入或更换收件人。

## 调度与投递

`proactive.py` 保存基础检查，滚动规划未来 26 小时，`plugin/proactive.mjs` 将其绑定到 DSH 原生任务。30 秒维护不调用模型。检查在原计划时间 30 分钟后过期。用户消息优先；取消、暂停、配置变更和过期会在准入、工具续接和投递前重新检查。

`notify_user(message)` 是主动消息的唯一投递入口，queued 只表示已入队。普通助手文本不单独转发。复用文字投递回执、微信预算保护、失败恢复和未知发送不重放规则。基础心跳不要求查目标、计划、行为或健康；具体事实按当前目的查询，不能根据历史督促指令恢复实验规则。

## 分支切换与数据

从包含实验功能的旧版或 experimental 切换到 main 时，保留 config.json 中的 policy、旧目标清单设置、activity_history 和 health_receiver 原值，但运行配置不含它们。保存 main 设置不会清除这些字段，切回 experimental 可继续使用。

proactive_state 保存当前运行配置；分支或配置变化会增加 revision，取消旧 planned/running/queued 检查及其跟进。原生调度协调器仅清除与这些托管检查绑定的任务，不修改用户创建的定时任务。已发送与未知发送的历史、Apple 操作账本、微信登录和健康数据保留。旧数据库 observation 列仅保留存储兼容性，main 不生成或读取观察内容用于通知。

## 验证

测试使用隔离配置、临时数据库和模拟微信/Apple/模型服务。覆盖基础问候不依赖实验工具、按需静默、固定通知、暂停/恢复、分支切换后旧检查取消、实验配置保留、定时任务隔离、上下文清理和未知投递不重放。
