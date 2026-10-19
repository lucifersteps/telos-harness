原始安装来源：Telos 已安装的 `baidu-ai-map`，ClawHub 版本 1.0.9。
官方仓库：https://github.com/baidu-maps/map-skills
许可证见 LICENSE，来自百度地图官方同名 Skill 的 MIT 许可证。

本目录在上游接口正文前内联当前 Harness 的运行规则。
Harness 原生 skill 工具发现和加载 SKILL.md；独立 baidu_maps 工具执行受控查询。
不执行文档中的 curl 或安装指令，不读取模型可见的 Token，也不另建 Skill 注册表。
接口与坐标来源校验位于 ../../checkout/src/telos/tools/baidu_maps.py。
