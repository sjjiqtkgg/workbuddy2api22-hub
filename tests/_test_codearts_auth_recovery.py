# -*- coding: utf-8 -*-
"""CodeArts 认证失败不再把账号打成「已停用」。

上游回 401/403 只说明手上这张临时凭证此刻不可用：反代应当先用一次性
refresh_token 换一张新凭证、在同一账号上就地重试；失败也只做指数退避冷却，
到期自动回到候选。只有 refresh_token 被上游明确拒绝时才进入 needs_relogin
（独立于面板「手动停用」），面板显示「需重新登录」，重新授权或刷新成功即自动清除。
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-auth-recovery-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
from _fake_codearts import start_server, stop_server  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def expiration_iso(seconds=7200):
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now + datetime.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def reset_accounts():
    """清空账号池与持久化状态，让每个用例从干净的一张表开始。"""
    C._POOLS.clear()
    folder = C.accounts_path(ACCOUNTS)
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.startswith("codearts-") or name == "state.json":
                os.remove(os.path.join(folder, name))


def seed_account(uid="u-auth", token="STALE-TOKEN", refresh="rt-old"):
    reset_accounts()
    auth = {
        "user_id": uid,
        "user_name": "账号认证",
        "domain_id": "d1",
        "cloud_dragon_token": token,
        "access_key_id": "AK",
        "secret_access_key": "SK",
        "expiration": expiration_iso(7200),
        "refresh_token": refresh,
        "code_verifier": "v",
        "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }
    auth = C.save_auth(ACCOUNTS, auth)
    pool = C.pool_for(ACCOUNTS, trigger_auto=False)
    acct = pool.add(auth)
    return pool, acct


def fresh_payload(uid, token="FRESH-TOKEN"):
    return {
        "user_id": uid,
        "user_name": "账号认证",
        "domain_id": "d1",
        "credentials": {
            "security_token": token,
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "expiration": expiration_iso(7200),
        },
        "refresh_token": "rt-new",
    }


def durable_state(uid):
    path = os.path.join(C.accounts_path(ACCOUNTS), "state.json")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    for row in rows:
        if row.get("name") == uid:
            return row
    return {}


def chat_body(session_key):
    return C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
    }, session_key)


srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

try:
    C.save_config(ACCOUNTS, {"enabled": True})
    cfg = C.load_config(ACCOUNTS)

    print("== 401 后强制刷新并就地重试 ==")
    state.reset_requests()
    state.token = "FRESH-TOKEN"
    state.chat_error = None
    state.oauth_error = None
    pool, acct = seed_account(token="STALE-TOKEN")
    stream, _acct, _model = C.open_chat(cfg, chat_body("sess-retry"), "sess-retry")
    text = b"".join(stream).decode("utf-8", "replace")
    stream.close()
    check("请求最终成功", "你好" in text, text[:120])
    check("只刷新一次", state.token_calls == 1, state.token_calls)
    check("凭证已换新", acct.auth.get("cloud_dragon_token") == "FRESH-TOKEN",
          acct.auth.get("cloud_dragon_token"))
    check("账号没有被停用", acct.disabled is False and acct.manual_disabled is False)
    check("不在待重新登录", acct.needs_relogin is False)
    check("可以继续被选中", acct.healthy() is True)
    check("磁盘上的凭证也已更新",
          C.load_auths(ACCOUNTS)[0].get("cloud_dragon_token") == "FRESH-TOKEN")

    print("== refresh_token 被拒：needs_relogin，而不是停用 ==")
    state.reset_requests()
    state.oauth_error = (400, {"error": "invalid_grant",
                               "error_msg": "refresh token has been used"})
    pool, acct = seed_account(token="STALE-TOKEN")
    raised = None
    try:
        C.open_chat(cfg, chat_body("sess-dead"), "sess-dead")
    except C.CodeArtsError as exc:
        raised = exc
    state.oauth_error = None
    check("向上抛错（不静默吞掉）", raised is not None)
    check("错误说明要重新登录",
          raised is not None and "重新登录" in str(raised), str(raised))
    check("标记为待重新登录", acct.needs_relogin is True)
    check("不是「已停用」", acct.disabled is False)
    check("不参与选号", acct.healthy() is False)
    check("面板视图暴露状态", acct.view().get("needs_relogin") is True)
    check("状态已落盘", durable_state(acct.uid).get("needs_relogin") is True)

    print("== 刷新成功但上游仍 401：只冷却，不拉黑 ==")
    state.reset_requests()
    state.oauth_error = None
    state.chat_error = (401, {"error_msg":
                              "Incorrect IAM authentication information: decrypt token fail"})
    state.chat_error_remaining = -1
    pool, acct = seed_account(token="STALE-TOKEN")
    try:
        C.open_chat(cfg, chat_body("sess-cool"), "sess-cool")
        raised = False
    except C.CodeArtsError:
        raised = True
    state.chat_error = None
    state.chat_error_remaining = -1
    check("本次请求失败", raised is True)
    check("账号没有被停用", acct.disabled is False)
    check("进入退避冷却", acct.cool_until > time.time(), acct.cool_until - time.time())
    check("冷却期内不选号", acct.healthy() is False)
    acct.cool_until = 0
    check("冷却到期自动回到候选（无需人工启用）", acct.healthy() is True)

    print("== 新凭证到手即自动清除待重新登录 ==")
    acct.needs_relogin = True
    acct.last_error = "refresh_token 已失效"
    acct.auth_err_count = 3
    acct.update_auth(fresh_payload(acct.uid))
    check("状态已清除", acct.needs_relogin is False)
    check("计数已清零", acct.auth_err_count == 0)
    check("重新可用", acct.healthy() is True)

    print("== 面板手动停用仍然优先 ==")
    pool.disable(acct.uid, "手动停用")
    acct.update_auth(fresh_payload(acct.uid))
    check("刷新不会解除手动停用", acct.disabled is True)
    check("手动停用标记保留", acct.manual_disabled is True)
    check("不参与选号", acct.healthy() is False)

    print("== /health 计数 ==")
    pool.enable(acct.uid)
    pool.get(acct.uid).needs_relogin = True
    C.invalidate_overview()
    health = C.health(C.load_config(ACCOUNTS), force=True)
    check("health 带 needs_relogin", health.get("needs_relogin") == 1, health)
    check("healthy 不把它算进去", health.get("healthy") == 0, health)

    print("== 401 刷新成功但就地重试命中积分耗尽：按积分冷却，不错记认证失败 ==")
    state.reset_requests()
    state.token = "FRESH-TOKEN"
    state.oauth_error = None
    state.chat_error = None
    state.embedded_error = {"error_code": "CREDIT_EXHAUSTED",
                            "error_msg": "credit exhausted"}
    pool, acct = seed_account(token="STALE-TOKEN")
    retry_exc = None
    try:
        C.open_chat(cfg, chat_body("sess-credit"), "sess-credit")
    except C.CodeArtsError as exc:
        retry_exc = exc
    state.embedded_error = None
    check("抛出积分耗尽错误", isinstance(retry_exc, C.CodeArtsUpstreamError)
          and retry_exc.code == "CREDIT_EXHAUSTED", retry_exc)
    check("未累加 auth_err_count", acct.auth_err_count == 0, acct.auth_err_count)
    check("冷却时长走 CREDIT_COOLDOWN",
          acct.cool_until - time.time() > C.AUTH_COOLDOWN + 60,
          acct.cool_until - time.time())

    print("== 并发 401 已由前序请求刷新：传入 failed_token 时不重复消费 refresh_token ==")
    state.reset_requests()
    state.token = "FRESH-TOKEN"
    pool, acct = seed_account(token="FRESH-TOKEN")
    before_token_calls = state.token_calls
    C._ensure_token(acct, force=True, failed_token="STALE-TOKEN")
    check("跳过重复刷新", state.token_calls == before_token_calls,
          (before_token_calls, state.token_calls))
finally:
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
