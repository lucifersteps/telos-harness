# 0.6.1-experimental.1 发布验证（2026-11-17）

从 experimental 构建完整实验版，包含微信/Apple/百度地图/DSH 原生能力、用户定时任务、基础心跳与问候，以及目标督促、可插拔策略、ActivityWatch/屏幕时间/健康查询、行为观察和健康接收端。共同修复仍包括 a5ff46a 与 750d812；main 核心版移除实验模块的提交不合入本分支。

## 产物

执行 `packaging/build.py --version 0.6.1-experimental.1 --reuse-apple`，使用 Python 3.14.6 / PyInstaller 6.21.0 重新构建 Python 组件。Swift 源码与 0.6.0 相同，Apple 应用逐文件复用已验证的 0.6.0 签名组件，保留旗标修复。

- 安装包：`telos-harness-0.6.1-experimental.1-macos-arm64.tgz`。
- SHA-256：`38ba1f9140b6aa56a4128a4ab30881d87239650282ac3b80303c82afc84d53bc`。
- 173 个归档文件，清单 172 项全部核验通过；780 个内嵌模块包含实验策略与 activity_history，归档包含健康接收端和行为工具。
- Apple、Python 与 SILK 签名校验通过，仍为 ad-hoc 签名。
- 检查 Git 历史、当前源码、归档、内嵌模块的常见凭据模式，未发现匹配；包内未发现本机构建者身份或临时目录残留。模式检查不等于完整安全审计。

## 回归

Python 全量收集 644 项：首轮 588 通过、50 跳过、6 项健康测试因独立工作树中缺少预期的相邻接收端源码而出现夹具错误。修正为可通过 `TELOS_TEST_HEALTH_ROOT` 指定源码后，健康接收端及 Desktop 生命周期 7 项全部复验通过，其中 6 项替代首轮错误，合计 **594 通过、50 跳过、零未解决失败**。没有重复计算额外复验的通过数。

JavaScript **68 项通过**。跳过项需要额外的真实账号、模型、系统授权或媒体环境，不计为通过。此次生产业务代码未因测试路径问题修改。

真实升级测试从公开 0.6.0 核心包创建用户定时任务，然后安装实验包并重启，检查配置、登录、历史、定时任务保留；activity_history 和行为观察参数恢复注册，旗标未知时普通提醒事项与日历仍可查询。

健康测试显式使用 `TELOS_TEST_HEALTH_ROOT` 指向接收端源码，只在临时目录写入合成数据，模型、微信与 Apple 均使用模拟服务。测试不读取生产健康数据库或令牌，不修改现有 Apple 数据。

## 发布与限制

发布为 GitHub prerelease，不替换最新正式版 0.6.0。实验模块依赖用户已有 ActivityWatch、屏幕时间和健康数据服务；模型行为判断仍可能有误，缺失数据不等于没有行动。

验证环境为 macOS 27.2 Apple Silicon、Python 3.14.6、官方 CLI 运行时与 DSH Desktop 0.2.2-rc.1。macOS 27.0.1 / DSH 0.2.2-rc.3 故障组合仍待实机验证；Developer ID 公证及另一台实体 Mac 首次安装尚未完成。
