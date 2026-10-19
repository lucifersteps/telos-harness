# Telos Apple 原生 Helper

这个 helper 是 Apple Reminders / Calendar 的导入导出后端。Telos Things 的事实源是
`runtime/things.sqlite`，Apple 只作为外部来源或目标。helper 使用 Apple 公开的 EventKit
framework 处理稳定的列表、提醒事项和日程操作。分区、标签、子任务、旗标状态等
Reminders 私有能力被隔离在这个 connector 内。写入使用 Apple 私有的
`ReminderKit.framework`；高保真读取以只读方式访问本机 Reminders CoreData SQLite store，
并把这些元数据合并到 EventKit 对象上。

这个 helper 只适用于 macOS，并要求宿主进程具备 Calendar 和 Reminders 的隐私权限。
它不使用 AppleScript，也不使用基于 shell 的脚本桥接。

本地构建：

```sh
swiftc -framework EventKit -framework AppKit -lsqlite3 telos_apple_helper.swift -o telos-apple-helper
```

Python wrapper 会这样调用 helper：

```sh
telos-apple-helper list < request.json
```

期望返回：

```json
{"ok": true, "data": {}}
```

支持的动作：

- `capabilities`
- `list`
- `detail`
- `create`
- `update`
- `delete`
