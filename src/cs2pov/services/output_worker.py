"""Bounded child CLI: reads output files, never opens SQLite or sends input."""
import argparse
import json
from pathlib import Path
import sys

from cs2pov.adapters.probe_tool import trusted_probe
from cs2pov.adapters.video import read_data_lease
from cs2pov.services.output_validation import (
    decode_discovery_request, decode_validation_request, discover_output_files,
    validate_output, output_json_value,
)
from cs2pov.storage.settings import DataError

MAX_WORKER_JSON = 2 * 1024 * 1024


def strict_json(data):
    if len(data) > MAX_WORKER_JSON:
        raise DataError('视频检查任务或结果超出大小限制。')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DataError('视频检查 JSON 包含重复字段。')
            result[key] = value
        return result
    def constant(_):
        raise DataError('视频检查 JSON 包含非有限数。')
    try:
        raw = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DataError('视频检查 JSON 无法读取。') from error
    if type(raw) is not dict:
        raise DataError('视频检查 JSON 不是对象。')
    return raw


def run_request(phase, raw, *, probe_path=None):
    if phase == 'discover':
        return discover_output_files(decode_discovery_request(raw))
    if phase == 'validate':
        request = decode_validation_request(raw)
        with trusted_probe(probe_path) as executable:
            return validate_output(request, ffprobe=executable)
    raise DataError('未知视频检查步骤。')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('discover', 'validate'), required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--probe', type=Path)
    parser.add_argument('--result', type=Path)
    args = parser.parse_args(argv)
    try:
        from cs2pov.adapters.worker_job import contain_current_worker
        contain_current_worker()
        with read_data_lease(args.request) as signature:
            if signature.bytes > MAX_WORKER_JSON:
                raise DataError('视频检查任务文件过大。')
            with args.request.open('rb') as stream:
                raw = strict_json(stream.read(MAX_WORKER_JSON + 1))
        result = run_request(args.phase, raw, probe_path=args.probe)
        content = json.dumps(output_json_value(result), ensure_ascii=True, allow_nan=False)
        if len(content.encode('utf-8')) > MAX_WORKER_JSON:
            raise DataError('视频检查结果超过大小限制。')
    except Exception as error:
        emit_result({'error': (str(error) or type(error).__name__)[:4096]}, args.result)
        return 1
    emit_result(output_json_value(result), args.result)
    return 0


def emit_result(value, path=None):
    from cs2pov.storage.transaction import atomic_write, no_redirection
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode('utf-8')
    if len(data) > MAX_WORKER_JSON: raise DataError('视频检查结果超过大小限制。')
    if path is not None:
        no_redirection(path)
        if path.exists(): raise DataError('结果文件已存在，禁止覆盖旧检查。')
        atomic_write(path, data)
    if sys.stdout is not None:
        print(data.decode('utf-8'), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
