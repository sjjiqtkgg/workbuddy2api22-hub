"""``claude-*`` model names: Claude Desktop can point at this gateway directly.

Claude Desktop's "third-party inference" panel only accepts a model id shaped
like ``claude-*``; the WorkBuddy upstream serves none of them, so the request
came back ``400 / 11102 model ... service info not found`` and the only way to
use the desktop app was a local forwarding layer that renamed the model. This
suite pins the prefix mapping that replaces that layer:

  1. the built-in table, the env overrides and the panel table, in that order;
  2. longest-prefix, case-insensitive resolution - only ``claude-*`` names are
     touched, so a real id (or an unknown one) still reaches the upstream as
     the client spelled it, and an unknown model still gets the upstream's 400;
  3. the reply names the *real* model id (a client reading the echoed name must
     not believe Claude answered), while the operator's exact alias table keeps
     its existing echo-the-alias behaviour;
  4. the outbound body really carries the resolved id, and the API key's model
     restriction is still checked against the name the client sent.

Run with: python tests/_test_claude_model_alias.py
No upstream credentials or network access are used.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="claude-alias-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as A
import wb_client_aliases as C
import wb_proxy as P
import wb_settings as S

SETTINGS_DIR = _startup_dir.name


class _Handler(object):
    """Minimal request handler: only what the alias hooks read."""

    def __init__(self, key="k1"):
        self._key = key
        self._client_model = ""
        self._claude_alias = ""

    def _key_id(self):
        return self._key


def _no_env(**overrides):
    """Clear every alias env var, then apply `overrides`."""
    env = {}
    for name in ("WB_CLAUDE_ALIASES", "CLAUDE_ALIAS_OPUS",
                 "CLAUDE_ALIAS_SONNET", "CLAUDE_ALIAS_HAIKU"):
        env[name] = os.environ.get(name, "")
    env.update(overrides)
    return mock.patch.dict(os.environ, env)


class TableTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claude-table-")
        self.addCleanup(setattr, P, "ACCOUNTS_DIR", P.ACCOUNTS_DIR)

    def test_built_in_table_matches_the_shipped_models(self):
        with _no_env():
            table = C.claude_table({})
        self.assertEqual(table["claude-opus"], "deepseek-v4-pro")
        self.assertEqual(table["claude-sonnet"], "deepseek-v4.1-flash")
        self.assertEqual(table["claude-haiku"], "deepseek-v4.1-flash")

    def test_family_env_overrides_one_entry_and_keeps_the_rest(self):
        with _no_env(CLAUDE_ALIAS_OPUS="hy4-preview-f"):
            table = C.claude_table({})
        self.assertEqual(table["claude-opus"], "hy4-preview-f")
        self.assertEqual(table["claude-sonnet"], "deepseek-v4.1-flash")

    def test_whole_table_env_replaces_the_defaults(self):
        with _no_env(WB_CLAUDE_ALIASES="claude-sonnet=glm-5.3"):
            table = C.claude_table({})
        self.assertEqual(table, {"claude-sonnet": "glm-5.3"})

    def test_json_table_is_accepted(self):
        raw = json.dumps({"claude-opus": "kimi-k3.1-pro"})
        with _no_env(WB_CLAUDE_ALIASES=raw):
            table = C.claude_table({})
        self.assertEqual(table["claude-opus"], "kimi-k3.1-pro")

    def test_settings_table_wins_over_env(self):
        with _no_env(CLAUDE_ALIAS_SONNET="glm-5.3"):
            table = C.claude_table({"claude-sonnet": "minimax-m3"})
        self.assertEqual(table["claude-sonnet"], "minimax-m3")

    def test_off_switch_empties_the_env_table(self):
        with _no_env(WB_CLAUDE_ALIASES="off"):
            self.assertEqual(C.claude_table({}), {})

    def test_identity_and_blank_entries_are_dropped(self):
        table = C.parse_pairs({"claude-opus": "claude-opus", " ": "x",
                               "claude-x": ""})
        self.assertEqual(table, {})

    def test_settings_switch_requires_opt_in_and_gates_the_table(self):
        with mock.patch.dict(os.environ, {"WB_CLAUDE_ALIASES_ENABLED": ""}):
            self.assertFalse(S.claude_aliases_enabled(self.dir))
        with mock.patch.dict(os.environ, {"WB_CLAUDE_ALIASES_ENABLED": "1"}):
            self.assertTrue(S.claude_aliases_enabled(self.dir))
        S.set_claude_aliases_enabled(self.dir, True)
        self.assertTrue(S.claude_aliases_enabled(self.dir))
        S.set_claude_aliases(self.dir, {"claude-sonnet": "glm-5.3"})
        S.set_claude_aliases_enabled(self.dir, False)
        self.assertEqual(S.effective_claude_aliases(self.dir), {})
        S.set_claude_aliases_enabled(self.dir, True)
        # env still layers under the stored table
        with _no_env():
            self.assertEqual(S.effective_claude_aliases(self.dir)["claude-sonnet"],
                             "glm-5.3")


class ResolveTests(unittest.TestCase):
    TABLE = {
        "claude-opus": "deepseek-v4-pro",
        "claude-sonnet": "deepseek-v4.1-flash",
        "claude-sonnet-4-5": "glm-5.3",
    }

    def test_prefix_matches_any_version_suffix(self):
        self.assertEqual(C.resolve_claude_model("claude-sonnet-5", self.TABLE),
                         "deepseek-v4.1-flash")
        self.assertEqual(C.resolve_claude_model("claude-3-5-sonnet-20241022",
                                                self.TABLE),
                         "deepseek-v4.1-flash")
        self.assertEqual(C.resolve_claude_model("claude-opus-4-1", self.TABLE),
                         "deepseek-v4-pro")

    def test_longest_prefix_wins(self):
        self.assertEqual(C.resolve_claude_model("claude-sonnet-4-5-20250929",
                                                self.TABLE), "glm-5.3")

    def test_matching_is_case_insensitive(self):
        self.assertEqual(C.resolve_claude_model("Claude-Sonnet-5", self.TABLE),
                         "deepseek-v4.1-flash")

    def test_real_and_unknown_names_are_not_touched(self):
        for name in ("deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3",
                     "opus", "sonnet", "haiku", "gpt-nonexistent", "", None):
            self.assertEqual(C.resolve_claude_model(name, self.TABLE), "", name)

    def test_apply_rewrites_only_the_model(self):
        payload = {"model": "claude-opus-4-1", "max_tokens": 16,
                   "messages": [{"role": "user", "content": "hi"}]}
        before, after = C.apply_claude_alias(payload, self.TABLE)
        self.assertEqual((before, after), ("claude-opus-4-1", "deepseek-v4-pro"))
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["max_tokens"], 16)

    def test_apply_is_a_noop_for_other_models(self):
        payload = {"model": "gpt-nonexistent"}
        self.assertEqual(C.apply_claude_alias(payload, self.TABLE), ("", ""))
        self.assertEqual(payload["model"], "gpt-nonexistent")


class HandlerHookTests(unittest.TestCase):
    def setUp(self):
        S.set_claude_aliases_enabled(SETTINGS_DIR, True)
        S.set_claude_aliases(SETTINGS_DIR, {})
        self.handler = _Handler()

    def test_hook_rewrites_and_records_the_alias(self):
        payload = {"model": "claude-sonnet-5"}
        with _no_env():
            out = P._apply_claude_model_alias(self.handler, payload)
        self.assertEqual(out, "deepseek-v4.1-flash")
        self.assertEqual(payload["model"], "deepseek-v4.1-flash")
        self.assertEqual(self.handler._claude_alias, "deepseek-v4.1-flash")

    def test_reply_echoes_the_real_id_not_the_claude_name(self):
        self.handler._client_model = "claude-sonnet-5"
        with _no_env():
            P._apply_claude_model_alias(self.handler, {"model": "claude-sonnet-5"})
        self.assertEqual(P.client_facing_model(self.handler,
                                               "deepseek-v4.1-flash"),
                         "deepseek-v4.1-flash")

    def test_operator_exact_alias_keeps_echoing_the_alias(self):
        # The two tables are independent: an exact operator alias is a name the
        # client itself chose, so the existing echo-the-alias rule stays.
        self.handler._client_model = "my-alias"
        self.assertEqual(P.client_facing_model(self.handler, "glm-5.3"),
                         "my-alias")

    def test_unknown_model_is_left_for_the_upstream_400(self):
        payload = {"model": "gpt-nonexistent"}
        with _no_env():
            self.assertEqual(P._apply_claude_model_alias(self.handler, payload), "")
        self.assertEqual(payload["model"], "gpt-nonexistent")
        self.assertEqual(self.handler._claude_alias, "")

    def test_switch_off_restores_the_old_behaviour(self):
        S.set_claude_aliases_enabled(SETTINGS_DIR, False)
        payload = {"model": "claude-sonnet-5"}
        with _no_env():
            self.assertEqual(P._apply_claude_model_alias(self.handler, payload), "")
        self.assertEqual(payload["model"], "claude-sonnet-5")
        S.set_claude_aliases_enabled(SETTINGS_DIR, True)


class _Sock(object):
    def settimeout(self, value):
        pass


class _Response(object):
    def __init__(self):
        self.closed = False

    @property
    def fp(self):
        sock = _Sock()

        class Raw(object):
            _sock = sock

        class Fp(object):
            raw = Raw()

        return Fp()

    def close(self):
        self.closed = True


class _Pool(object):
    affinity = type("Affinity", (), {
        "unbind": lambda self, key: None,
        "demote": lambda self, key, uid=None: None})()

    def __init__(self, accounts):
        self.accounts = accounts

    def count_ready(self, realm, model=None):
        return len(self.accounts)

    def pick_for_session(self, realm, session_key=None, exclude=(),
                         model=None, page=None):
        for account in self.accounts:
            if account.uid not in exclude:
                return account
        return None

    def get(self, uid):
        return next((a for a in self.accounts if a.uid == uid), None)

    def apply_remaining_weights(self, weights=None):
        return weights or {}


class OutboundTests(unittest.TestCase):
    """End to end through open_upstream: the body really carries the real id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = A.Account(
            {"uid": "u-cn-alias", "accessToken": "t", "realm": "cn"},
            os.path.join(self.tmp.name, "account.json"))
        old_pool = P.POOL
        P.POOL = _Pool([self.account])
        self.addCleanup(setattr, P, "POOL", old_pool)
        for patch in (mock.patch.object(P, "apply_daily_guards", return_value=0),
                      mock.patch.object(P, "remaining_priority_enabled",
                                        return_value=False),
                      mock.patch.object(P, "upstream_timeouts",
                                        return_value=(2, 2)),
                      mock.patch.object(P, "model_reasoning_meta",
                                        side_effect=lambda m: {})):
            patch.start()
            self.addCleanup(patch.stop)

    def sent(self, payload):
        seen = {}
        handler = _Handler()

        def urlopen(req, timeout=30, proxy=""):
            body = json.loads(req.data.decode("utf-8"))
            seen.update(body)
            return _Response()

        with _no_env(), mock.patch.dict(os.environ, {"WB_EFFORT_ALIASES_ENABLED": "1"}), \
                mock.patch.object(A, "urlopen", side_effect=urlopen):
            # Resolve the incoming name before the outbound body is built.
            P._apply_claude_model_alias(handler, payload)
            P._normalize_client_effort(handler, payload)
            response, _account, _effort = P.open_upstream(
                payload, target_realm="cn")
        self.addCleanup(response.close)
        return seen

    def test_claude_name_reaches_the_upstream_as_the_real_id(self):
        seen = self.sent({"model": "claude-sonnet-5",
                          "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(seen["model"], "deepseek-v4.1-flash")

    def test_xhigh_reaches_the_upstream_as_max(self):
        seen = self.sent({"model": "claude-sonnet-5", "reasoning_effort": "xhigh",
                          "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(seen["reasoning_effort"], "max")

    def test_unknown_model_is_not_rewritten(self):
        seen = self.sent({"model": "gpt-nonexistent",
                          "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(seen["model"], "gpt-nonexistent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
