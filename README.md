# 虾米 POV

Windows 本地 CS2 Demo 片段录制助手，使用 NVIDIA App 录制。

## 最终交付版

本轮指定的完整便携包为 `xiamipov_main.zip`，版本标识 `v2026.10.07`。下载与校验信息见 [GitHub Release](https://github.com/3081542838-cloud/ytxxm-csgo-project/releases/tag/v2026.10.07) 和 [交付清单](docs/releases/v2026.10.07.json)。解压整个文件夹后运行 `虾米pov.exe`。

## 当前功能

- 导入本地 Demo，选择玩家、回合或多杀片段。
- 使用带击杀标记的可拖动时间轴，按秒调整片段。
- 暖珊瑚桌面界面和虾图标。
- 临时部署仿实战 HUD，以 `-insecure` 启动本地回放，定位片段并自动发送录制热键。
- 结束后退出本次游戏并校验恢复；异常中断时保留恢复备份。
- 完整便携包附带经校验 HUD，启动无需联网；不包含 ffprobe 设置和控制台按键设置。

## 使用条件与当前限制

需要 Windows、Steam、CS2、NVIDIA 显卡及 NVIDIA App。应用中的保存目录和热键应与 NVIDIA 设置一致。

这是使用者指定的最终交付版本；“最终”表示这次交付的文件已固定，不代表所有电脑的实机录制均已验收。 部分环境下 Windows 会拒绝模拟输入（例如错误码 5），此时自动录制无法开始。程序会报告失败并执行安全收尾；自动测试通过不代表每台电脑都能录制。应用不保证游戏更新后的 HUD 兼容性，也不作账号安全的绝对保证。

## 运行与构建

开发环境为 Python 3.12。安装锁定依赖后运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m cs2pov.app
```

构建完整便携包：

```powershell
.\.venv\Scripts\python.exe scripts\build_portable.py
```

构建输出位于 `.build/portable-*/package/XiamiPOV`。必须使用整个文件夹，不能只移动 EXE。首次运行生成 `data` 配置，不改用其他人的个人设置。

## 测试

```powershell
.\scripts\test.ps1
```

完整套件包含依赖本机真实 Demo 和独立视频样本的集成测试；这些个人素材不上传。缺少样本时会明确失败，不会静默跳过。详见 [构建说明](BUILD.md)。

## 数据与许可

Git 仓库包含源码、测试、设计文件、依赖锁和 HUD 的必要许可材料，不包含个人 Demo、录制视频、运行数据、恢复备份及构建缓存。便携 EXE/ZIP 超过常规 GitHub Git 单文件大小限制，通过 GitHub Release 附件提供，本地保存在 `dist/portable`。

HUD 参考资源用于个人及朋友的非商业使用，必须保留 [许可](resources/bundled/LICENSE)、[第三方说明](resources/bundled/THIRD_PARTY_LICENSES.md) 和 [改动说明](resources/bundled/CHANGES.txt)。该许可不自动授予其他项目代码或第三方依赖的权利。
