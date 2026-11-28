"""推理强度归一化：客户端发 xhigh / ultra，上游收到 max。

## 现场

Codex 桌面端的推理滑杆只有 low / medium / high / xhigh 四档，最高档发出去就是
`xhigh`；Claude 桌面端用 `thinking.budget_tokens >= 32000` 表达同一件事，网关把它
翻成 `xhigh`（`anthropic_effort_from_messages`）。而上游的 DeepSeek 构建
（`deepseek-v4.1-flash`）只发布 `low / high / max`。

不管的话有两条路，两条都不对：上游直接 400，或者被
`map_reasoning_effort` 当成「模型不支持的档位」改写成**模型默认档** `high` ——
客户端要求「比滑杆最高还高」，拿回来的是中间档。

## 修法

`wb_client_aliases.normalize_effort()` 在 `map_reasoning_effort` 之前把别名档位
改写成该模型真正运行的档位，并打一行 `effort_from=… effort_to=…`。模型自己声明了
这个档位时保持原样（`deepseek-v4-pro` 发布 `high / xhigh`，改成 `max` 反而是降级）。

## 这里钉住的

  1. 默认表 / 环境变量 / 面板表三层优先级，以及 `off` 的关闭语义；
  2. 三种字段拼法（`reasoning_effort` / `reasoningEffort` / `reasoning.effort`）；
  3. 模型声明了该档位就不改写；没声明就改成目标档位；
  4. `build_upstream_body` 里的顺序：先归一化，再交给 catalog 兜底，`max` 不会被
     改回 `high`；调用方传入的 payload 不被污染；
  5. 日志行含 `effort_from` / `effort_to`；
  6. Anthropic 的 `budget_tokens` 一路走到出站 body 也是 `max`。

Run with: python tests/_test_effort_alias.py
No upstream credentials or network access are used.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="effort-alias-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_client_aliases as C
import wb_proxy as P
import wb_settings as S

SETTINGS_DIR = _startup_dir.name
FLASH = {"supportedEfforts": ["low", "high", "max"], "defaultEffort": "high"}
PRO = {"supportedEfforts": ["high", "xhigh"], "defaultEffort": "high"}


def _no_env(**overrides):
    env = {"WB_EFFORT_ALIASES": os.environ.get("WB_EFFORT_ALIASES", "")}
    env.update(overrides)
    return mock.patch.dict(os.environ, env)


class _Handler(object):
    def __init__(self, key="k1"):
        self._key = key

    def _key_id(self):
        return self._key


class TableTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="effort-table-")

    def test_default_table_maps_the_two_invented_levels(self):
        with _no_env():
            table = C.effort_table({})
        self.assertEqual(table, {"xhigh": "max", "ultra": "max"})

    def test_env_replaces_the_default_table(self):
        with _no_env(WB_EFFORT_ALIASES="xhigh:high"):
            today = C.effort_table({})
        self.assertEqual(today, {"xhigh": "high"})

    def test_off_disables_the_table(self):
        with _no_env(WB_EFFORT_ALIASES="off"):
            self.assertEqual(C.effort_table({}), {})

    def test_settings_override_one_key_and_keep_the_rest(self):
        with _no_env():
            table = C.effort_table({"xhigh": "high"})
        self.assertEqual(table["xhigh"], "high")
        self.assertEqual(table["ultra"], "max")

    def test_settings_switch_requires_opt_in(self):
        with mock.patch.dict(os.environ, {"WB_EFFORT_ALIASES_ENABLED": ""}):
            self.assertFalse(S.effort_aliases_enabled(self.dir))
        with mock.patch.dict(os.environ, {"WB_EFFORT_ALIASES_ENABLED": "1"}):
            self.assertTrue(S.effort_aliases_enabled(self.dir))
        S.set_effort_aliases_enabled(self.dir, False)
        self.assertEqual(S.effective_effort_aliases(self.dir), {})
        S.set_effort_aliases_enabled(self.dir, True)
        with _no_env():
            self.assertEqual(S.effective_effort_aliases(self.dir),
                             {"xhigh": "max", "ultra": "max"})

    def test_table_size_is_capped(self):
        with self.assertRaises(ValueError):
            S.set_effort_aliases(self.dir, dict(
                ("level%d" % i, "max") for i in range(40)))


class NormalizeTests(unittest.TestCase):
    TABLE = {"xhigh": "max", "ultra": "max"}

    def normalized(self, body, supported=None, table=None):
        body = dict(body)
        before, after = C.normalize_effort(
            body, body.get("model"), self.TABLE if table is None else table,
            supported)
        return body, before, after

    def test_flat_key_is_rewritten(self):
        body, before, after = self.normalized(
            {"model": FLASH, "reasoning_effort": "xhigh"}, FLASH["supportedEfforts"])
        self.assertEqual((before, after), ("xhigh", "max"))
        self.assertEqual(body["reasoning_effort"], "max")

    def test_camel_case_key_keeps_its_spelling(self):
        body, _before, _after = self.normalized(
            {"model": FLASH, "reasoningEffort": "ultra"}, FLASH["supportedEfforts"])
        self.assertEqual(body["reasoningEffort"], "max")
        self.assertNotIn("reasoning_effort", body)

    def test_responses_nested_effort_is_rewritten(self):
        body, _before, _after = self.normalized(
            {"model": FLASH, "reasoning": {"effort": "xhigh"}},
            FLASH["supportedEfforts"])
        self.assertEqual(body["reasoning"]["effort"], "max")

    def test_nested_reasoning_object_is_not_shared_with_the_caller(self):
        original = {"model": FLASH, "reasoning": {"effort": "xhigh", "summary": "auto"}}
        out, _before, _after = self.normalized(original, FLASH["supportedEfforts"])
        self.assertEqual(original["reasoning"]["effort"], "xhigh")
        self.assertEqual(out["reasoning"]["summary"], "auto")

    def test_model_that_declares_the_level_keeps_it(self):
        body, before, after = self.normalized(
            {"model": PRO, "reasoning_effort": "xhigh"}, PRO["supportedEfforts"])
        self.assertEqual((before, after), ("", ""))
        self.assertEqual(body["reasoning_effort"], "xhigh")

    def test_unknown_catalog_still_maps(self):
        body, _before, _after = self.normalized({"model": "whatever",
                                                 "reasoning_effort": "xhigh"}, [])
        self.assertEqual(body["reasoning_effort"], "max")

    def test_levels_outside_the_table_are_untouched(self):
        for level in ("low", "medium", "high", "max", "none"):
            body, before, _after = self.normalized(
                {"model": FLASH, "reasoning_effort": level},
                FLASH["supportedEfforts"])
            self.assertEqual(before, "", level)
            self.assertEqual(body["reasoning_effort"], level)

    def test_missing_or_non_string_effort_is_untouched(self):
        for body in ({"model": FLASH}, {"model": FLASH, "reasoning_effort": 3},
                     {"model": FLASH, "reasoning": {}}):
            before, _after = C.normalize_effort(
                body, FLASH, self.TABLE, FLASH["supportedEfforts"])
            self.assertEqual(before, "")


class HandlerHookTests(unittest.TestCase):
    def setUp(self):
        S.set_effort_aliases_enabled(SETTINGS_DIR, True)
        S.set_effort_aliases(SETTINGS_DIR, {})
        self.handler = _Handler()

    def test_hook_rewrites_and_logs_both_levels(self):
        lines = []
        payload = {"model": "deepseek-v4.1-flash", "reasoning_effort": "xhigh"}
        with _no_env(), \
                mock.patch.object(P, "model_reasoning_meta",
                                  return_value=dict(FLASH)), \
                mock.patch.object(P, "log", side_effect=lambda msg, **kw: lines.append(msg)):
            P._normalize_client_effort(self.handler, payload)
        self.assertEqual(payload["reasoning_effort"], "max")
        line = next(m for m in lines if "effort_from=" in m)
        self.assertIn("effort_from=xhigh", line)
        self.assertIn("effort_to=max", line)
        self.assertIn("model=deepseek-v4.1-flash", line)

    def test_hook_is_silent_when_nothing_changes(self):
        lines = []
        payload = {"model": "deepseek-v4.1-flash", "reasoning_effort": "high"}
        with _no_env(), \
                mock.patch.object(P, "model_reasoning_meta",
                                  return_value=dict(FLASH)), \
                mock.patch.object(P, "log", side_effect=lambda msg, **kw: lines.append(msg)):
            P._normalize_client_effort(self.handler, payload)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(lines, [])

    def test_switch_off_keeps_the_client_value(self):
        S.set_effort_aliases_enabled(SETTINGS_DIR, False)
        payload = {"model": "deepseek-v4.1-flash", "reasoning_effort": "xhigh"}
        with _no_env(), mock.patch.object(P, "model_reasoning_meta",
                                          return_value=dict(FLASH)):
            P._normalize_client_effort(self.handler, payload)
        self.assertEqual(payload["reasoning_effort"], "xhigh")
        S.set_effort_aliases_enabled(SETTINGS_DIR, True)


class UpstreamBodyTests(unittest.TestCase):
    """build_upstream_body: normalisation must win over the catalog fallback."""

    MESSAGES = [{"role": "user", "content": "hi"}]

    def setUp(self):
        S.set_effort_aliases_enabled(SETTINGS_DIR, True)
        S.set_effort_aliases(SETTINGS_DIR, {})
        S.set_reasoning_effort_map(SETTINGS_DIR, True)

    def build(self, payload, meta=None):
        with _no_env(), mock.patch.object(P, "model_reasoning_meta",
                                          side_effect=lambda m: dict(meta or {})):
            return P.build_upstream_body(payload)

    def test_xhigh_becomes_max_instead_of_the_model_default(self):
        body = self.build({"model": "deepseek-v4.1-flash", "reasoning_effort": "xhigh",
                           "messages": self.MESSAGES}, FLASH)
        self.assertEqual(body["reasoning_effort"], "max")

    def test_ultra_becomes_max(self):
        body = self.build({"model": "deepseek-v4.1-flash", "reasoning_effort": "ultra",
                           "messages": self.MESSAGES}, FLASH)
        self.assertEqual(body["reasoning_effort"], "max")

    def test_high_is_passed_through(self):
        body = self.build({"model": "deepseek-v4.1-flash", "reasoning_effort": "high",
                           "messages": self.MESSAGES}, FLASH)
        self.assertEqual(body["reasoning_effort"], "high")

    def test_model_that_declares_xhigh_keeps_it(self):
        body = self.build({"model": "deepseek-v4-pro", "reasoning_effort": "xhigh",
                           "messages": self.MESSAGES}, PRO)
        self.assertEqual(body["reasoning_effort"], "xhigh")

    def test_caller_payload_is_not_mutated(self):
        payload = {"model": "deepseek-v4.1-flash", "reasoning_effort": "xhigh",
                   "messages": self.MESSAGES}
        self.build(payload, FLASH)
        self.assertEqual(payload["reasoning_effort"], "xhigh")

    def test_responses_style_body_is_rewritten_before_translation(self):
        responses = {"model": "deepseek-v4.1-flash", "input": "hi",
                     "reasoning": {"effort": "xhigh"}}
        with _no_env(), mock.patch.object(P, "model_reasoning_meta",
                                          side_effect=lambda m: dict(FLASH)):
            chat = P.responses_to_chat(responses)
        self.assertEqual(chat["reasoning_effort"], "xhigh")
        # the handler hook already normalised the payload before translation;
        # simulate that order and check the outbound value
        with _no_env(), mock.patch.object(P, "model_reasoning_meta",
                                          side_effect=lambda m: dict(FLASH)):
            P._normalize_client_effort(object(), responses)
            chat = P.responses_to_chat(responses)
        self.assertEqual(chat["reasoning_effort"], "max")

    def test_anthropic_budget_reaches_the_body_as_max(self):
        payload = {"model": "deepseek-v4.1-flash", "max_tokens": 16,
                   "thinking": {"type": "enabled", "budget_tokens": 40000},
                   "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(P.anthropic_effort_from_messages(payload), "xhigh")
        chat = P.messages_to_chat(payload)
        self.assertEqual(chat["reasoning_effort"], "xhigh")
        body = self.build(chat, FLASH)
        self.assertEqual(body["reasoning_effort"], "max")

    def test_thinking_budget_band_table_is_documented_and_consistent(self):
        self.assertEqual(C.thinking_budget_effort(1024), "low")
        self.assertEqual(C.thinking_budget_effort(8192), "medium")
        self.assertEqual(C.thinking_budget_effort(20000), "high")
        self.assertEqual(C.thinking_budget_effort(40000), "xhigh")
        self.assertEqual([level for _limit, level in C.THINKING_BUDGET_BANDS],
                         ["xhigh", "high", "medium", "low"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
