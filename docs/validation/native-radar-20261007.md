# 官方方形 Demo 雷达验证

日期：2026-10-07。使用者选择 A：直接使用官方方形雷达，接受原生 Demo 可能显示双方位置。原定制 POV 敌情专项停止，不宣称原 A23 已通过。

## 实现范围

- `HudPreset.show_radar` 严格布尔，默认 false；旧预设及旧任务缺少字段时为 false。预设文件写 schema 2，schema 1 打开时不改写，第一次保存保留原字节备份。
- HUD 页提供“显示官方方形雷达”，支持保存、复制、重启、恢复默认。忙碌时禁止编辑；草稿保留冻结参数，修改预设后需要重新保存片段。
- 开启时从认证基包移除雷达父面板的隐藏条目，不改变原地图/图标内容；`cl_radar_square_when_spectating 1` 在 `cl_drawhud_force_radar 1` 前发送。
- 关闭时保留原隐藏资源及 `cl_drawhud_force_radar -1`。无自绘脚本或轨迹数据部署，无额外雷达解析，不读取地图 VPK，不复制地图素材。
- 仍使用受管理本地 `-insecure` 启动及九个小文件的备份恢复。原基包哈希、三路径白名单与资源总字节长度不变，资源来源与显示模式按完整重建核验。

## 分阶段测试

- 预设阶段：30 项通过，包含旧值、原始备份、严格布尔、未来版本阻断、复制和快照独立。
- 资源与命令阶段：131 项通过，包含认证重建、模式篡改阻断、命令顺序及原回放回归。
- 界面与任务阶段：124 项通过，包含重启、已冻结草稿不被预设更改、任务构建传参和忙碌禁用。
- 打包及配置检查阶段：49 项通过，包含预设 schema 2 缺字段不能伪装成旧数据。

## 实机

本机 CS2 1.41.8.9，使用生产 PreviewSession 和默认资源构建器，不使用实验雷达 builder。没有触发 NVIDIA 录制。

| 地图 | 私有证据目录 | 实际观察 | 恢复 |
| --- | --- | --- | --- |
| Mirage | `local-validation/native-radar/mirage-1791373223` | 方形地图、原生双方编号标记；隐藏后地图消失，重新显示恢复；播放中人物、位置、死亡标记动态更新 | complete，九个目标原哈希一致，原 Demo 不变 |
| Ancient | `local-validation/native-radar/ancient-1791373496` | 方形原地图和原生双方图标；暂停时正常，播放中坐标、人数与死亡标记动态更新 | complete，九个目标原哈希一致，原 Demo 不变 |

真实画面通过 Computer Use 读取 CS2 窗口核对。直接发送 resume 后不再处于正式已准备片段状态，private probe 记录 preview_changed；这是无录制实验中的预期状态变化，不作为正式录制通过。

`ancient-1791373353` 私有状态 JSON 与读操作发生 WinError 32，已完整恢复，未作为画面通过证据。修正私有状态记录对短暂读锁的处理后重新执行；未放宽产品输入或恢复门槛。

此前中断的 `de_mirage-2-1791363928` 已从固定目标与核验备份恢复，九个目标原哈希一致。日志与失败记录保留。

## 完整回归与交付

完整报告：`.reports/region-full-9a8ace7e63b543e7ae1f9b48cf9de7c5/summary.json`。完整收集 2397 项，分区集合一致；JUnit 含子用例 2401。失败、错误、跳过均为 0。`pip check` 通过。源码实际 HUD 包检查通过，覆盖隐藏与原生方形两种资源生成、认证、预设重启保存。

便携构建：`.build/portable-faa9755b5ae14c788a93a53c7cbf0747`，实际 EXE 检查：`.reports/portable-verification-b5742f834ac3419f84bdc45ec9fe9614/verification.json`。该检查验证原生依赖、实际 HUD 两种资源、预设重启和原 Demo 只读解析。新的 dist 应用已实际打开，HUD 页官方方形雷达开关与说明可见，原配置和旧草稿已加载。

交付报告：`.reports/native-radar-delivery.json`。新 EXE、ZIP、使用说明和 SHA256SUMS 已替换到 dist/portable；工作 data 在替换前后所有文件哈希相同。朋友用 ZIP 的 data 为空，ZIP CRC 通过。旧 EXE 和 ZIP 保留在独立构建备份。完整交付哈希见 `docs/releases/v2026.10.07-native-radar.md`。

第二台电脑录制仍由用户验收；本轮原生地图验证不等同于重新完成 NVIDIA 录制验收。

交付完成后再次运行全套测试，最终报告：`.reports/region-full-08d3edd7c0be4630b300c46694328cb3/summary.json`。2397 项完整收集，JUnit 2401，失败 0、错误 0、跳过 0，全部分区退出码 0。
