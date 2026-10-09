"""Assemble a full portable candidate with explicit external resource evidence."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
_spec = importlib.util.spec_from_file_location('portable_onefile_build', Path(__file__).with_name('build_onefile.py'))
build_onefile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_onefile)

base = build_onefile.base


def copy_verified(source, target, expected_sha256, expected_bytes):
    source = base._plain(source)
    if source.stat().st_size != expected_bytes or base.file_sha256(source) != expected_sha256:
        raise base.BuildError('随包资源不是已审核版本：' + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise base.BuildError('不能覆盖资源：' + str(target))
    shutil.copyfile(source, target)
    if target.stat().st_size != expected_bytes or base.file_sha256(target) != expected_sha256:
        raise base.BuildError('随包资源复制校验失败。')


def assemble(root, candidate, executable):
    sys.path.insert(0, str(root / 'src'))
    folder = candidate / 'package' / 'XiamiPOV'
    folder.mkdir(parents=True)
    copy_verified(executable, folder / '虾米pov.exe', base.file_sha256(executable), executable.stat().st_size)
    (folder / 'resources').mkdir()
    shutil.copytree(candidate / 'licenses', folder / 'licenses/application')
    from cs2pov.storage.local_resources import HUD_SHA256, HUD_BYTES
    copy_verified(root / 'resources/bundled/pov.vpk',
                  folder / 'resources/pov.vpk', HUD_SHA256, HUD_BYTES)
    reference = root / 'resources/bundled'
    from cs2pov.services.resource_setup import HUD_SOURCES
    for key, name in (('REFERENCE_LICENSE.txt', 'LICENSE'),
                      ('REFERENCE_THIRD_PARTY_LICENSES.md', 'THIRD_PARTY_LICENSES.md')):
        source = HUD_SOURCES[key]
        copy_verified(reference / name,
                      folder / 'licenses' / ('hud-reference-' + name),
                      source.sha256, source.size)
    (folder / 'licenses/hud-CHANGES.txt').write_text(
        'Required Notice: Copyright (c) 2026 DrEAmSs59\n'
        'Reference commit: f05c698c755dc7806855bb13a5c823dbf6a21de6\n'
        'Changes: Removed input audio, alerts, voice/input/radar tracks and extra playback controls; HUD visibility only.\n'
        'Personal/noncommercial use. Source game resource rights are not independently granted by the application.\n', encoding='utf-8')
    (folder / 'data').mkdir()
    base._exclusive_json(folder / 'portable.json', {'schema': 1, 'application': 'XiamiPOV'})
    (folder / '使用说明.txt').write_text(
        '虾米pov — 完整便携版\n\n'
        '请解压整个文件夹后双击 虾米pov.exe，不要只移动 exe。无需 Python。\n'
        '请放在纯英文路径，例如 D:\\XiamiPOV；CS2 无法可靠写入中文路径下的控制日志。应用文件名仍为虾米pov.exe。\n'
        '主界面和 HUD 无需联网：已审核 HUD VPK 随包提供，启动不会下载资源。\n'
        'HUD 保存在 resources；许可证和来源说明保存到 licenses。\n'
        '设置、HUD预设、片段记录和恢复备份在 data，首次启动自动生成配置。\n'
        '请安装本机 Steam、CS2 和 NVIDIA App，并在设置中填写本机路径、视频保存目录和快捷键。\n'
        'NVIDIA 保存目录需与你在应用中设置的一致，开始任务前确保当前未录制。\n'
        '导入 Demo，选玩家和片段，保存草稿，点击开始录制。任务期间保持 CS2 前台并松开键鼠。\n'
        '应用会在本地回放中临时部署 HUD，结束后退出本次游戏并恢复文件。\n'
        'HUD 设置中可选“显示官方方形雷达”，默认关闭；使用原生 Demo 雷达，可能显示双方位置。\n'
        '修改 HUD 后先保存预设、设为默认，再重新保存片段草稿；已保存草稿保留原任务参数。\n'
        '给朋友请发送初始压缩包，不要把已使用的 data 和恢复备份发给别人。\n'
        '仅供个人及朋友非商业使用；第二台电脑的实际录制仍需本机验收。\n', encoding='utf-8')
    rows = [{ 'path': file.relative_to(folder).as_posix(), 'bytes': file.stat().st_size,
              'sha256': base.file_sha256(file)} for file in sorted(folder.rglob('*')) if file.is_file()]
    base._exclusive_json(candidate / 'portable-inventory.json', {'schema': 1, 'files': rows,
        'personal_data_included': False, 'resources': 'bundled-reviewed-hud'})
    return folder


def main():
    root = Path(__file__).resolve().parents[1]
    candidate = root / '.build' / ('portable-' + uuid.uuid4().hex)
    args = build_onefile.command(root, candidate)
    before = base.source_manifest(root)
    candidate.mkdir(parents=True)
    base.collect_licenses(candidate / 'licenses', base.read_lock(root / 'requirements.lock'))
    with (candidate / 'build.log').open('xb') as log:
        result = subprocess.run(args, cwd=root, env=base.clean_build_environment(),
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            timeout=1200, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or base.source_manifest(root) != before:
        raise base.BuildError('构建失败或构建中源码变化，请查看 ' + str(candidate / 'build.log'))
    executable = candidate / 'dist' / 'XiamiPOV.exe'
    build_onefile.audit_archive(executable)
    audit = candidate / 'native-audit'; audit.mkdir()
    shutil.copyfile(candidate / 'work/XiamiPOV/PKG-00.toc', audit / 'COLLECT-00.toc')
    base._exclusive_json(candidate / 'native-audit.json', base.audit_native_sources(audit, required=True))
    folder = assemble(root, candidate, executable)
    base._exclusive_json(candidate / 'build-result.json', {'folder': str(folder), 'source': before})
    print(json.dumps({'candidate': str(candidate), 'folder': str(folder)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
