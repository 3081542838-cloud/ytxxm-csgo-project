# 第三方资源与依赖资料

核对日期：2026-10-09。本次整理保留现有材料，不修改应用或移除来源声明。

## HUD 来源

当前 `resources/bundled/pov.vpk` 来自 DrEAmSs59/CS2-insight-agent 固定提交 `f05c698c755dc7806855bb13a5c823dbf6a21de6` 提供的资源包，经提取和脚本替换重建。

- `hudteamcounter.vcss_c`、`hudteamcounter-equipmentinfo.vcss_c` 与参考包对应内容一致。
- `huddemocontroller.vts_c` 保留编译资源结构，其原始注入脚本替换为本项目脚本。
- 当前包为 410,376 字节，SHA256 为 `98cffb9688c227a5f8edba50508e25cef0568a9e541ba6024d9d0eac24828af9`。
- 保留 [原作者许可证](../../resources/bundled/LICENSE)、[原项目第三方说明](../../resources/bundled/THIRD_PARTY_LICENSES.md) 和 [来源与改动声明](../../resources/bundled/CHANGES.txt)。

原项目第三方说明是来源附带材料，不等于本应用的实际依赖清单。本应用的依赖版本见 [dependencies.json](dependencies.json) 和 [requirements.lock](../../requirements.lock)，包来源及哈希见 [source-checksums.json](source-checksums.json)。

## 尚待核实

1. HUD 中基础游戏资源与其他内容的完整分发权利尚未核实；原作者许可不能自动代替其他权利人的授权。
2. 部分 Python wheel 未提供完整的许可文本。现有构建仅复制检测到的许可文件与包信息，不能据此断言所有材料齐全。
3. Qt/PySide6 等依赖的适用许可证、对应库源码提供安排与其他分发条件仍需完整核对。

现有 HUD 按非商业用途保留原许可条件。本文不宣称资源全部原创，也不宣称授权审查已经完成。

资料入口：[PolyForm Noncommercial 条款](https://polyformproject.org/licenses/noncommercial/1.0.0)、[Qt 分发条件说明](https://www.qt.io/development/open-source-lgpl-obligations)、[本地资源审查](../validation/stage-1-resource-review.md)。
