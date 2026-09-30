# 项目进度存档

存档日期：2026-09-30

项目目录：`E:\csgo-project`

开发分支：`codex/pov-first-version`

状态：用户要求暂停存档；随后提出“继续在这个文件里面工作”，目标文件或目录待澄清。已停止本次依赖安装会话，不继续产品开发或实机输入操作。

## 已确认的产品与设计

- 需求依据：`PRD.md`。
- 执行计划：`DEVELOPMENT_PLAN.md`，按 T01—T24 顺序及 G1—G3 门槛推进。
- Windows 本地 Demo 回放，NVIDIA 应用录制，一次一个片段。
- 个人实战和简洁 POV 两种仿实战 HUD，用户自行选择。
- 界面选择 B：浅色背景、蓝色强调。
- 图标采用 `assets/branding/shrimp-app-icon-v2.png`：白底虾，无视频标志；工作区 v1 已删除。
- 账号安全、原配置保护、失败阻断及恢复是硬门槛，不承诺绝不封号。

## 当前开发进度

### T01：部分完成

- 已核对参考项目 PolyForm Noncommercial 和 demoparser2 MIT 许可证。
- 已建立环境记录模板、依赖授权清单和 A01—A16 验收状态表。
- 已识别 CS2 安装目录及版本 1.41.8.6。
- 本机为 RTX 4060 Laptop GPU，NVIDIA 应用文件版本为 128.4.13.34。
- 用户给出的 Demo 已复制到忽略的本地实验目录，副本与原文件 SHA-256 一致。
- 已安装项目内 Python 3.12 虚拟环境；Rust 安装曾启动，暂停时已停止会话，完整性仍待检查。
- 尚未完成解析依赖版本记录、NVIDIA 录制配置读取及第二台电脑安排。

### T02：模拟恢复实验测试通过

代码：`experiments/recovery/transaction.py`

测试：`experiments/recovery/test_transaction.py`

已执行命令：

```powershell
& '.\.tools\parser-venv\Scripts\python.exe' -m unittest experiments.recovery.test_transaction -v
```

最后运行结果：24 项测试，全部通过，无跳过。

覆盖：精确及幂等恢复、原本同名文件保护、路径白名单与越界、Windows junction、日志和备份损坏、外部文件冲突、并发锁、权限/磁盘故障注入、真实子进程退出、部署及恢复中断、暂存文件残留。

初次运行发现路径允许非法分号及测试未指定 UTF-8 的问题，已修复并保留测试。新增的崩溃暂存文件检查也已通过。没有删除、跳过测试或放宽验收条件。

这只是强制限制在模拟游戏目录内的 Python 实验。不能直接用于真实游戏资源部署；正式 Rust 迁移、句柄级竞态保护及真实游戏恢复仍未验证。测试通过不等于 A11—A14 实机验收通过。

### T03—T24：未完成

- 已读取 Computer Use 技能及相关安全指引，初始化 Windows 工具。
- 已通过工具打开用户预先安装的 NVIDIA 应用，但未修改其设置或触发录制。
- 未启动 CS2；未部署 HUD；未改动真实游戏配置、Steam 永久启动参数、GSI 文件或游戏资源。
- G1、G2、G3 均未通过；未创建正式 Tauri/React 产品工程，不应跳过前置门槛。

## 本地资料和运行状态

- `experiments/private/sample.dem`：用户 Demo 的实验副本。
- `docs/validation/private/machine-1.json`：真实环境、来源路径、样本 hash。
- `.tools/parser-venv/`：实验 Python 环境。
- `.tools/cargo/` 与 `.tools/rustup/`：本次工具链安装资料；不保证安装完整。
- 以上目录被 `.gitignore` 排除，保留在本机，不上传或提交比赛文件及个人环境详情。
- 依赖安装 exec 会话 2888 已终止；随后检查未发现 rustup-init/rustup/pip/python 安装进程。
- NVIDIA 应用由本次验证打开，未主动关闭用户应用。

## 恢复开发时的顺序

1. 先确认用户最新所指文件或目录；保持已确认 PRD 与 UI 选择。
2. 检查 Git 状态、工具链完整性和后台进程；重新运行 T02 的 24 项测试。
3. 补齐 T01 的解析依赖和录制配置证据；第二台电脑不足继续保持验收未通过。
4. 推进 T03 不改 HUD 的本地回放隔离验证，再执行 T04 解析与回放控制。
5. 执行 T05/T06 实机 HUD 和 NVIDIA 录制验证，修复失败后完成 T07。
6. G1—G3 全通过后才进入 T08 正式工程。后续按计划逐阶段测试，全部完成时运行所有测试。

恢复任何未完成游戏文件事务前须确认 CS2 已关闭。当前没有本产品产生的真实游戏文件事务。
