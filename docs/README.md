# 项目文档目录

当前产品使用官方方形 Demo 雷达。以下入口区分当前要求、验证范围和历史实验。

## 使用与开发

| 文档 | 用途 |
| --- | --- |
| [使用指南](../USER_GUIDE.md) | 首次设置、选择片段、雷达和录制操作 |
| [当前进度](../CURRENT_PROGRESS.md) | 当前交付、验证范围和未解决事项 |
| [产品需求](../PRD.md) | 已确认的产品范围与验收要求 |
| [开发计划](../DEVELOPMENT_PLAN.md) | 开发阶段与测试要求 |
| [构建说明](../BUILD.md) | Python 环境、运行、测试和便携包构建 |
| [恢复说明](../RECOVERY.md) | 异常后游戏文件与录制状态处理 |
| [交付清单](../RELEASE_CHECKLIST.md) | 已完成及未完成的交付检查 |

## 当前验证与发行

- [v2026.10.09 版本说明](releases/v2026.10.09.md)、[机器可读交付信息](releases/v2026.10.09.json)。
- [2026-10-09 仓库同步检查](validation/repository-sync-20261009.md)。
- [官方方形雷达验证](validation/native-radar-20261007.md)。
- [验收范围](validation/acceptance-matrix.md)。
- [第三方资源与依赖资料](licenses/README.md)。

## 设计与历史

- [界面和图标记录](../design/UI_DECISIONS.md)。
- [多杀与雷达计划](superpowers/2026-10-02-multikill-radar-plan.md)。
- [自动录制设计](superpowers/specs/2026-10-05-automatic-recording-design.md)。
- [已停止的自绘 POV 雷达设计](superpowers/specs/2026-10-07-pov-radar-implementation.md)、[历史实机实验](validation/pov-radar-live-20261007.md)。这些不是当前雷达方案或已完成的实战敌情验收。
- [HUD 来源审查记录](validation/stage-1-resource-review.md)、[原资源结构清单](validation/pov-source-audit.json)。
- [历史 v2026.10.07 发行说明](releases/v2026.10.07.md)、[后续本地雷达更新](releases/v2026.10.07-native-radar.md)。

详细本机日志、截图、私有 Demo 和视频保留在本地 `local-validation/`。本轮归档位置为 `local-validation/archive/20261009-repository-cleanup/`，不进入公开 Git 仓库。既有源码、实验模块和回归测试仍保留；当前用户流程不会部署自绘雷达轨迹。
