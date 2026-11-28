#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每 Key 累计积分额度（credit_limit）。

钉住四件事：
  1. 累计积分按 usage.jsonl 的 `key` 折叠 `credit`：支持增量续读（只读新追加的
     字节）、尾部半行不重复计入、日志轮转（文件被替换）后自动整份重建；
  2. settings 归一口径：0 / 负数 / 脏值 = 不限，正数保留两位小数；老面板不带
     该字段时保存必须沿用已存值，不能把用户设过的额度清成 0；
  3. 判定口径：已用 >= 上限才拦，limit <= 0 永不拦；统计本身出错时**放行**而不是
     把整池请求挡死（宁可少拦一次，不可误杀）；
  4. 接线：chat 与图片 / 视频两条派发路径都要调判定，面板要有输入框和徽标。

运行：python3 tests/_test_key_credit_quota.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_keyquota
import wb_proxy
import wb_settings
import wb_usage_io

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def write_rows(path, rows, mode="w"):
    with io.open(path, mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class KeyCreditTotals(unittest.TestCase):
    """usage.jsonl -> 每个 Key 的累计积分。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-keyquota-")
        self.log = os.path.join(self.dir, "usage.jsonl")
        wb_keyquota.reset()

    def tearDown(self):
        wb_keyquota.reset()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_sums_credit_per_key(self):
        write_rows(self.log, [
            {"key": "aaa", "credit": 1.5},
            {"key": "bbb", "credit": 2},
            {"key": "aaa", "credit": 0.5},
            {"key": None, "credit": 99},        # 没有 key 的行不参与折叠
            {"key": "aaa", "credit": "bad"},    # 脏 credit 跳过，不影响其它行
            {"key": "aaa"},                     # 缺 credit 视为 0
        ])
        totals = wb_keyquota.totals(self.dir)
        self.assertAlmostEqual(totals.get("aaa"), 2.0)
        self.assertAlmostEqual(totals.get("bbb"), 2.0)
        self.assertNotIn(None, totals)
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 2.0)
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "missing"), 0.0)

    def test_incremental_append_does_not_double_count(self):
        write_rows(self.log, [{"key": "aaa", "credit": 3}])
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 3.0, places=6)
        # 第二轮：新增整行 + 半行
        write_rows(self.log, [{"key": "aaa", "credit": 4}], mode="a")
        with io.open(self.log, "a", encoding="utf-8") as fh:
            fh.write('{"key": "aaa", "credit": 5')      # 半行，先不计
        wb_keyquota.reset()
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 7.0, places=6)
        # 补完半行：同一批字节只能算一次
        with io.open(self.log, "a", encoding="utf-8") as fh:
            fh.write('.0}\n')
        wb_keyquota.reset()
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 12.0, places=6)

    def test_partial_line_not_counted_twice(self):
        write_rows(self.log, [{"key": "aaa", "credit": 1}])
        with io.open(self.log, "a", encoding="utf-8") as fh:
            fh.write('{"key": "aaa", "credit": 2')
        first = wb_keyquota.used(self.dir, "aaa")     # 只有完整的 1.0
        self.assertAlmostEqual(first, 1.0, places=6)
        with io.open(self.log, "a", encoding="utf-8") as fh:
            fh.write('.0}\n')
        wb_keyquota.reset()
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 3.0, places=6)

    def test_rotation_rebuilds_from_scratch(self):
        write_rows(self.log, [{"key": "aaa", "credit": 9}])
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 9.0)
        replacement = self.log + ".replacement"
        write_rows(replacement, [{"key": "aaa", "credit": 0.25}])
        os.replace(replacement, self.log)
        self.assertAlmostEqual(wb_keyquota.used(self.dir, "aaa"), 0.25, places=6)

    def test_sealed_segments_and_active_tail_share_an_offset(self):
        write_rows(self.log, [{"key": "aaa", "credit": 3}])
        self.assertTrue(wb_usage_io.rotate(self.log, 1))
        write_rows(self.log, [{"key": "aaa", "credit": 4}], mode="a")
        self.assertEqual(wb_keyquota.refresh(self.dir), {"aaa": 7.0})
        self.assertTrue(wb_usage_io.rotate(self.log, 1))
        with io.open(self.log, "a", encoding="utf-8") as fh:
            fh.write('{"key": "aaa", "credit": 5}')
        self.assertEqual(wb_keyquota.refresh(self.dir), {"aaa": 7.0})
        with io.open(self.log, "a", encoding="utf-8") as fh:
            fh.write('\n')
        self.assertEqual(wb_keyquota.refresh(self.dir), {"aaa": 12.0})

    def test_env_switch_disables_accounting(self):
        write_rows(self.log, [{"key": "aaa", "credit": 5}])
        with mock.patch.dict(os.environ, {"WB_KEY_CREDIT_TOTALS": "0"}):
            self.assertEqual(wb_keyquota.used(self.dir, "aaa"), 0.0)
            self.assertEqual(wb_keyquota.totals(self.dir), {})


class SettingsNormalization(unittest.TestCase):
    """credit_limit 的归一与保存口径。"""

    def test_clean_entry_normalizes_credit_limit(self):
        cases = [
            (None, 0.0),
            ("", 0.0),
            (0, 0.0),
            (-5, 0.0),
            ("bad", 0.0),
            ("inf", 0.0),
            (1000, 1000.0),
            ("12.345", 12.35),
            (0.5, 0.5),
        ]
        for raw, want in cases:
            entry = wb_settings._clean_key_entry(
                {"key": "sk-test-credit", "credit_limit": raw})
            self.assertIsNotNone(entry)
            self.assertAlmostEqual(entry["credit_limit"], want, places=6,
                                   msg="credit_limit=%r" % (raw,))

    def test_cleaner_keeps_other_fields(self):
        entry = wb_settings._clean_key_entry({
            "key": "sk-test-credit", "name": "unit", "credit_limit": 100,
            "daily_token_limit": 5, "concurrency_limit": 2,
        })
        self.assertEqual(entry["name"], "unit")
        self.assertEqual(entry["daily_token_limit"], 5)
        self.assertEqual(entry["concurrency_limit"], 2)
        self.assertAlmostEqual(entry["credit_limit"], 100.0)


class GatewayGate(unittest.TestCase):
    """key_credit_exhausted 的判定口径。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-gate-")
        self.log = os.path.join(self.dir, "usage.jsonl")
        wb_keyquota.reset()
        write_rows(self.log, [{"key": "kid", "credit": 10}])
        self.patch = mock.patch.object(wb_proxy, "USAGE_DIR", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        wb_keyquota.reset()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unlimited_never_blocks(self):
        for limit in (0, None, ""):
            hit, used, eff = wb_proxy.key_credit_exhausted({"credit_limit": limit}, "kid")
            self.assertFalse(hit)
            self.assertEqual(eff, 0.0)

    def test_blocks_only_when_used_reaches_limit(self):
        hit, used, limit = wb_proxy.key_credit_exhausted({"credit_limit": 11}, "kid")
        self.assertFalse(hit)
        self.assertAlmostEqual(used, 10.0)
        self.assertAlmostEqual(limit, 11.0)

        hit, used, limit = wb_proxy.key_credit_exhausted({"credit_limit": 10}, "kid")
        self.assertTrue(hit, "已用 == 上限 必须拦（>= 口径）")

        hit, _used, _limit = wb_proxy.key_credit_exhausted({"credit_limit": 9.99}, "kid")
        self.assertTrue(hit, "上限低于已用必须立刻拦")

    def test_unknown_key_is_zero_used(self):
        hit, used, _limit = wb_proxy.key_credit_exhausted({"credit_limit": 1}, "nobody")
        self.assertFalse(hit)
        self.assertAlmostEqual(used, 0.0)

    def test_accounting_failure_fails_open(self):
        """统计炸了不能把请求挡死：宁可放行。"""
        with mock.patch.object(wb_keyquota, "used", side_effect=RuntimeError("boom")):
            hit, _used, _limit = wb_proxy.key_credit_exhausted({"credit_limit": 1}, "kid")
        self.assertFalse(hit)


class Wiring(unittest.TestCase):
    """接线检查：少接一处，这个 Key 的额度就会形同虚设。"""

    def _read(self, name):
        with io.open(os.path.join(ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_gate_called_on_every_dispatch_path(self):
        src = self._read("wb_proxy.py")
        dispatch = src.split("    def do_POST(self):", 1)[1]
        guard = dispatch.index("key_credit_exhausted(")
        for next_step in ("self._maybe_web_model(", "self._handle_audio_post(",
                          "self._handle_media_post(", "self._dispatch_chat_post("):
            self.assertLess(guard, dispatch.index(next_step))
        self.assertIn('"credit_limit"', src)
        self.assertIn('"credit_used"', src)
        self.assertIn("wb_keyquota.used(", src)

    def test_panel_has_inputs_and_badge(self):
        dash = self._read("dashboard.html")
        self.assertIn("editKeyCredit_", dash)
        self.assertIn("editKeyCreditRemain_", dash)
        self.assertIn("credit_used", dash)
        self.assertIn("credit_limit", dash)

    def test_old_panel_save_keeps_stored_limit(self):
        """请求体没带 credit_limit 时沿用已存值（否则老面板一保存就清零）。"""
        src = self._read("wb_proxy.py")
        window = src.split('if "credit_limit" in item:', 1)
        self.assertEqual(len(window), 2, "保存路径必须单独处理 credit_limit 缺省")
        self.assertIn('stored.get("credit_limit")', window[1][:400])


if __name__ == "__main__":
    unittest.main(verbosity=2)
