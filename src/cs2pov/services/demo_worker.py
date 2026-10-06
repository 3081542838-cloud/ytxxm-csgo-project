"""Isolated parser process: cancellation cannot freeze or kill the desktop."""
import argparse
import json
import hashlib
import sys
from pathlib import Path
from cs2pov.adapters.demo import fingerprint, parse_native, DemoError
from cs2pov.storage.transaction import atomic_write, no_redirection


CACHE_VERSION = "analysis-v2-controller1-demoparser-0.42.0-"


def analysis_digest(analysis):
    return hashlib.sha256(json.dumps(analysis, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True).encode("utf-8")).hexdigest()


def verify_cache(raw):
    analysis = raw["analysis"]
    if raw["analysis_sha256"] != analysis_digest(analysis):
        raise DemoError("分析缓存校验失败，请重新解析。")
    expected = {"schema", "parser", "map", "tick_rate", "duration", "timeline", "players", "rounds", "deaths", "kills", "kill_issues"}
    if set(analysis) != expected or analysis["schema"] != 2 or analysis["parser"] != "demoparser2-0.42.0":
        raise DemoError("分析缓存结构或版本无效。")
    if not analysis["timeline"] or not analysis["players"] or not analysis["map"]:
        raise DemoError("分析缓存缺少有效比赛数据。")
    return analysis


def analyse(path, cache_directory, *, parser=parse_native):
    before = fingerprint(path)
    no_redirection(cache_directory)
    cache_directory.mkdir(parents=True, exist_ok=True)
    cache = cache_directory / (CACHE_VERSION + before["sha256"] + ".json")
    analysis = None
    if cache.exists():
        no_redirection(cache)
        if cache.stat().st_size <= 32 * 1024 * 1024:
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if raw["fingerprint"]["sha256"] == before["sha256"] and raw["analysis"]["schema"] == 2:
                    analysis = verify_cache(raw)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Cache is disposable; the original Demo is authoritative.
    if analysis is None:
        analysis = parser(path)
    after = fingerprint(path)
    if before != after:
        raise DemoError("解析期间 Demo 内容变化，结果已丢弃，请重试。")
    raw = {"fingerprint": after, "analysis": analysis, "analysis_sha256": analysis_digest(analysis)}
    content = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(content) > 32 * 1024 * 1024:
        raise DemoError("Demo 分析结果过大，第一版无法安全缓存。")
    atomic_write(cache, content)
    return {"cache": str(cache), "fingerprint": after}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    try:
        result = analyse(args.demo, args.cache)
    except BaseException as error:
        emit({"error": str(error) or type(error).__name__}, args.result)
        return 1
    emit(result, args.result)
    return 0


def emit(result, path=None):
    content = json.dumps(result, ensure_ascii=True, allow_nan=False).encode('utf-8')
    if path is not None:
        no_redirection(path)
        if path.exists(): raise DemoError('解析结果已存在，禁止覆盖旧检查。')
        atomic_write(path, content)
    if sys.stdout is not None: print(content.decode('utf-8'), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
