# 界面与图标设计记录

日期：2026-09-30

用户已选择 B：浅色简洁风格。参考图为 `home-style-b.png`，用于确认视觉方向；示例比赛画面、录制记录和状态不是已实现功能。

正式页面沿用浅色背景、蓝色强调、清晰文字及轻量卡片，导航与功能遵守 PRD。具体交互仍需要实现与测试，不能从效果图推断功能已完成。

已确认采用图标：`shrimp-app-icon-v2.png`（与本文件同目录）。根据用户修正，背景改为白色圆角底，移除中央视频及播放符号，保留白色与珊瑚色虾造型。PNG 外部透明。用户确认使用第二张，并要求删除第一张；工作区中的 v1 图标已删除。阶段 4 已导出多尺寸 ICO，使用者确认原版窗口及图标显示正常。

2026-10-01 范围更新：第一版只做一种仿实战 HUD，隐藏雷达；不提供简洁模式切换。HUD 页面名称为“HUD 设置”。首页参考图只保留视觉方向，正式实现使用虾图标和 PRD 中的真实状态，不沿用样图的示例数据。

新版编辑使用内置 imagegen，提示词如下：

```text
Use case: precise-object-edit. Edit this app icon with only these requested changes: replace the BLUE rounded-square tile with a clean WHITE rounded-square tile, and completely remove the video camera symbol and its play triangle from the center. Leave the center as empty white negative space. Preserve the shrimp's pose, curled segmented silhouette, head, dark eye, coral antennae, coral segment accents and tail, placement and scale. Preserve the rounded-square outline and transparent exterior around it. Make the tile white, not light blue. Adjust the existing white shrimp's soft neutral-gray shading minimally so it remains visible against the white tile, avoiding blue rim light. No blue background, no video camera, no play triangle, no new symbol, no letters, no text, no extra elements. High-resolution polished standalone icon, opaque white inside the rounded-square tile, genuinely transparent outside.
```

## 2026-10-01 界面美化修正

使用者确认功能界面显示正常，但明确认为初版视觉太丑，要求使用 ui-ux-pro-max 美化。按已确认 B 方向修正现有五页：醒目主标题、紧凑环境状态栏、当前任务与画面/保存两列、下方录制记录。统一本地绘制的线性图标、字体、留白、控件和焦点状态。保留批准的虾图标；展示真实空状态，不把参考图中的比赛图片、假记录和已取消第二模式带入产品。

已运行技能 design-system 和 UX 空状态搜索。采用清晰层次、可读对比度、明确空状态和稳定交互；搜索自动推荐的视频宣传背景、橙色 CTA 和在线字体不适合本地桌面工具，未采用。真实组件截图见 local-validation/phase4-ui/refined/；860×620、200% 渲染见 refined-high-dpi/。新视觉等待使用者查看；不是新功能或架构重设计。

使用者随后回复“暂时先这样继续”，采用此视觉继续后续阶段。

生成方式：内置 imagegen；以下为生成图标所用提示词。

```text
Use case: logo-brand. Asset type: Windows desktop application icon for a local CS2 POV video recording utility with a clean light UI and blue accent. Create ONE polished standalone app icon, square centered composition, genuinely transparent background outside the icon artwork. Primary motif is a clearly recognizable SHRIMP, friendly but sleek and professional, curved segmented body, distinctive head and two simplified antennae, with an elegant tail fan. Build a single cohesive memorable symbol by integrating a small video recording lens or subtle play triangle into the negative space of the shrimp's curled body. The shrimp must remain the dominant visual and recognizable at 32px. Use a compact white and soft coral shrimp silhouette on a rich clean blue rounded-square tile (#3568C8 to #2454AD subtle restrained tonal variation), soft 20 percent corner radius and very subtle inset dimensionality. Coral only a small accent, not entire busy illustration. Thick smooth shapes, minimal segment details, antennae contained within tile, generous internal padding 15 percent, crisp bold silhouette, premium restrained desktop utility icon. Clean modern icon design compatible with a light white/blue productivity interface. No text, no letters, no numbers, no Nvidia or Counter Strike logos, no guns, no esports mascot aggression, no black outline, no scene, no mockup or device frame, no multiple variants, no tiny detailed claws, no long fine lines, no outer drop shadow. Show only the full icon at high resolution with transparent surrounding canvas.
```
