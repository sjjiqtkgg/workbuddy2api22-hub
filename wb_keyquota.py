"""每 Key 累计积分统计（配合 API Key 的 credit_limit 额度）。

积分来自 usage.jsonl 里每行的 ``credit``（上游按模型倍率实际扣的分数），
按 ``key``（API Key 的 id）折叠累加，用于「这个 Key 一共只能用 N 积分」的额度判断。

实现采用「增量续读 + 必要时整份重建」：
- 首次访问、文件被轮转替换（dev/ino 变化或变短）时，读完整逻辑日志重建，
  已封存的段（usage.jsonl.segments/）通过 wb_usage_io.open_log 一起读到；
- 其余情况只读上次偏移之后新追加的字节，单个请求的开销与日志总长无关。

环境变量 ``WB_KEY_CREDIT_TOTALS=0`` 可整体关闭统计（此时额度判断视为不限）。
"""

import json
import math
import os
import threading

import wb_usage_io

_LOCK = threading.Lock()
_STATE = {"log": None, "identity": (0, 0), "offset": 0, "totals": {}}


def enabled():
    raw = (os.environ.get("WB_KEY_CREDIT_TOTALS") or "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def log_path(usage_dir):
    return os.path.join(usage_dir, "usage.jsonl")


def _apply(line, totals):
    try:
        row = json.loads(line)
        if not isinstance(row, dict) or not row.get("key"):
            return
        credit = float(row.get("credit") or 0)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return
    if math.isfinite(credit) and credit:
        key = str(row["key"])
        totals[key] = totals.get(key, 0.0) + credit


def _read(path, offset, end, totals):
    """Consume only complete lines within the logical log snapshot."""
    if offset == end:
        return offset
    with wb_usage_io.open_log(path, "rb") as handle:
        handle.seek(offset)
        while offset < end:
            line = handle.readline(end - offset)
            if not line.endswith(b"\n"):
                break
            _apply(line, totals)
            offset += len(line)
    return offset


def refresh(usage_dir):
    """Return totals at a logical offset shared by sealed and active segments."""
    if not enabled():
        return {}
    path = log_path(usage_dir)
    with _LOCK:
        end, dev, ino = wb_usage_io.log_key(path)
        identity = (dev, ino)
        reset_needed = (_STATE["log"] != path or _STATE["identity"] != identity
                        or end < _STATE["offset"])
        offset = 0 if reset_needed else _STATE["offset"]
        if not reset_needed and end == offset:
            return dict(_STATE["totals"])
        totals = {} if reset_needed else dict(_STATE["totals"])
        offset = _read(path, offset, end, totals)
        _STATE.update(log=path, identity=identity, offset=offset, totals=totals)
        return dict(totals)


def used(usage_dir, key_id):
    """这个 Key 从建库以来的累计积分（拿不到就当 0）。"""
    if not key_id or not enabled():
        return 0.0
    try:
        return float(refresh(usage_dir).get(str(key_id), 0.0))
    except Exception:
        return 0.0


def totals(usage_dir):
    return refresh(usage_dir)


def reset():
    """丢掉缓存，下次访问整份重建（换日志目录、手工修数据后可用）。"""
    with _LOCK:
        _STATE.update(log=None, identity=(0, 0), offset=0, totals={})
