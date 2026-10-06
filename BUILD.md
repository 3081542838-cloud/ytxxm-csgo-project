# 构建和验证

## 环境

Windows 10/11、Python 3.12。Steam、CS2、NVIDIA App 由使用者在目标电脑安装。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
```

## 源码运行与测试

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m cs2pov.app
.\scripts\test.ps1
```

完整测试包括原始 Demo 和开发期间的真实视频/资源集成资料；这些私人资料不上传 GitHub。新电脑仅克隆代码无法完成全部实机资料测试，缺失资料必须报告失败，不能跳过后声称完整通过。单元测试、UI 测试及公开脱敏日志均在 tests 下。历史日志的账号路径已脱敏，事件、时间和状态字段保持原值，清单区分原始来源哈希与公开样本哈希。

## 当前便携构建入口

```powershell
.\.venv\Scripts\python.exe scripts\build_portable.py
```

脚本在 .build 下创建独立候选目录，生成 XiamiPOV 文件夹及构建清单。构建包含 PyInstaller 单文件程序、resources、licenses、portable.json 和空 data 目录。发送给朋友时应发送整个文件夹的压缩包；不要发送已经使用过的 data。目标电脑无需 Python，首次打开无需下载 HUD。

HUD 来自 resources/bundled/pov.vpk，构建核对固定 SHA256，许可和修改说明同时保留。仅限符合 PolyForm Noncommercial 许可的非商业使用；详见该目录的 LICENSE 和 THIRD_PARTY_LICENSES.md。

scripts/build.py 是保留的历史内部 onedir 验证入口，不能替代当前便携构建入口。ffprobe 相关旧诊断模块和测试仅用于开发回归，不在当前用户界面或便携依赖中。

## 验证范围

自动测试验证解析、片段边界、状态转换、文件备份恢复、异常阻断和界面。便携包另有原生依赖和隔离启动检查。它们不能证明每台电脑 NVIDIA 热键都能录制。

目前仍存在部分 Windows 环境拒绝自动按键（错误码 5）的问题；应用阻断录制并恢复，不应将这个版本描述为所有电脑自动录制验收完成。遇到失败保留日志和恢复备份，不通过删除测试或放宽门槛取得成功。

GitHub 保存源码、设计、测试、依赖锁定和审核后的 HUD。EXE/ZIP、私人 Demo、录制视频、用户数据、备份及本地报告不进入 Git 仓库。大于 100MB 的成品不能直接提交普通 Git 仓库，应单独作为发行附件交付。
