#!/usr/bin/env python3
"""One-command acceptance for the claude-* aliases and effort normalisation.

    python scripts/acceptance_claude_effort.py

Starts a stand-in upstream (canned SSE reply; a model id it does not know gets
the real upstream's ``400 / 11102``), starts this checkout's gateway against it
with a throw-away account, then replays the checks from
``docs/CLAUDE-DIRECT-AND-EFFORT.md`` and prints what the *upstream* received.

Nothing here touches a real account or the network: the point is to observe the
request the gateway puts on the wire, which the panel log cannot show for every
field (and which an unpatched deployment cannot show at all).

Exit status: 0 all checks pass, 1 something failed, 2 the gateway did not start.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "acceptance-key"

# Model ids the CN catalog actually serves (from /v1/models on a live box).
KNOWN = {
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4.1-flash-sg",
    "glm-5.3", "hy4-preview-f", "hy3", "kimi-k3.1-pro", "minimax-m3",
    "sonnet", "opus", "haiku",
}

RECORDS = []


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        RECORDS.append({
            "model": body.get("model"),
            "reasoning_effort": body.get("reasoning_effort")
            or body.get("reasoningEffort"),
            "thinking": body.get("thinking"),
        })
        model = str(body.get("model") or "")
        if model not in KNOWN:
            payload = json.dumps({
                "code": 11102,
                "msg": "model [%s] service info not found" % model,
            }).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        frames = [
            {"id": "chatcmpl-accept", "object": "chat.completion.chunk",
             "model": model,
             "choices": [{"index": 0, "delta": {"role": "assistant",
                                                "content": "ok"}}]},
            {"id": "chatcmpl-accept", "object": "chat.completion.chunk",
             "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                       "total_tokens": 6}},
        ]
        for frame in frames:
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def post(url, body, headers=None, timeout=60):
    """POST and return (status, body text); HTTP errors are data, not failures."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + KEY)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_health(base, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def last():
    return RECORDS[-1] if RECORDS else {}


def main():
    tmp = tempfile.mkdtemp(prefix="wb-acceptance-")
    accounts = os.path.join(tmp, "accounts")
    usage = os.path.join(tmp, "usage")
    os.makedirs(accounts, exist_ok=True)
    os.makedirs(usage, exist_ok=True)
    with open(os.path.join(accounts, "u-accept.json"), "w", encoding="utf-8") as fh:
        json.dump({"uid": "u-accept", "accessToken": "acceptance-token",
                   "realm": "cn", "platform": "CLI", "product": "workbuddy",
                   "nickname": "acceptance", "enabled": True}, fh)

    up_port = free_port()
    gw_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env.update({
        "ACCOUNTS_DIR": accounts,
        "WB_PROXY_USAGE_DIR": usage,
        "API_KEY": KEY,
        "WB_DEPLOY_MODE": "local",
        "WB_PROXY_DEFAULT_REALM": "cn",
        "WB_CN_CHAT_UPSTREAM": "http://127.0.0.1:%d" % up_port,
        "WB_ENABLE_ANTHROPIC_MESSAGES": "1",
        "WB_CLAUDE_ALIASES_ENABLED": "1",
        "WB_EFFORT_ALIASES_ENABLED": "1",
        "WB_TLS_FINGERPRINT": "python",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    out_path = os.path.join(tmp, "gateway.log")
    err_path = os.path.join(tmp, "gateway.err.log")
    out = open(out_path, "w", encoding="utf-8")
    err = open(err_path, "w", encoding="utf-8")
    gateway = subprocess.Popen(
        [sys.executable, "-m", "wb_proxy", "--port", str(gw_port)],
        cwd=ROOT, env=env, stdout=out, stderr=err)
    base = "http://127.0.0.1:%d" % gw_port
    results = []

    def check(cid, what, expected, actual):
        ok = expected == actual
        results.append((cid, what, expected, actual, "PASS" if ok else "FAIL"))
        print("[%s] %s: expected=%s actual=%s" % (cid, what, expected, actual))
        return ok

    try:
        if not wait_health(base):
            print("gateway did not start; tail of log:")
            out.flush()
            err.flush()
            with open(err_path, encoding="utf-8", errors="replace") as fh:
                print("".join(fh.readlines()[-20:]))
            return 2
        print("gateway ready: %s\n" % base)
        anthropic = {"anthropic-version": "2023-06-01"}

        def messages(model, path="/v1/messages", extra=None):
            body = {"model": model, "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}]}
            if extra:
                body.update(extra)
            return post(base + path, body, anthropic)

        print("== A: claude-* model aliases ==")
        status, text = messages("claude-sonnet-5")
        check("A1", "/v1/messages claude-sonnet-5 -> 200, reply names the real id",
              "deepseek-v4.1-flash", json.loads(text).get("model"))
        check("A1b", "upstream received the real id",
              "deepseek-v4.1-flash", last().get("model"))
        status, text = messages("claude-opus-4-1")
        check("A2", "claude-opus-4-1 -> deepseek-v4-pro",
              "deepseek-v4-pro", json.loads(text).get("model"))
        check("A2b", "upstream received the opus target",
              "deepseek-v4-pro", last().get("model"))
        status, text = messages("claude-3-5-sonnet-20241022")
        check("A2c", "Claude 3.x id (version first) also resolves",
              "deepseek-v4.1-flash", json.loads(text).get("model"))
        status, _text = messages("gpt-nonexistent")
        check("A3", "unknown model is not silently mapped (upstream 400)",
              "400", str(status))
        status, _text = messages("claude-sonnet-5", "/v1/messages/count_tokens")
        check("A4", "/v1/messages/count_tokens accepts a claude name",
              "200", str(status))
        status, _text = messages("claude-haiku-4-5-20251001",
                                 "/v1/messages/count_tokens")
        check("A4b", "count_tokens accepts claude-haiku-4-5", "200", str(status))

        print("\n== B: reasoning effort normalisation ==")

        def responses(effort):
            return post(base + "/v1/responses", {
                "model": "deepseek-v4.1-flash", "input": "hi",
                "reasoning": {"effort": effort}, "stream": False})

        status, _text = responses("xhigh")
        check("B1", "/v1/responses reasoning.effort=xhigh -> 200",
              "200", str(status))
        check("B1b", "upstream received max", "max", last().get("reasoning_effort"))
        status, _text = responses("high")
        check("B2", "reasoning.effort=high -> 200", "200", str(status))
        check("B2b", "upstream received high unchanged",
              "high", last().get("reasoning_effort"))
        responses("ultra")
        check("B3", "ultra is mapped as well", "max", last().get("reasoning_effort"))
        status, _text = post(base + "/v1/chat/completions", {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "xhigh", "stream": False})
        check("B4", "/v1/chat/completions reasoning_effort=xhigh -> 200",
              "200", str(status))
        check("B4b", "upstream received max", "max", last().get("reasoning_effort"))
        status, _text = post(base + "/v1/messages", {
            "model": "claude-sonnet-5", "max_tokens": 64,
            "thinking": {"type": "enabled", "budget_tokens": 40000},
            "messages": [{"role": "user", "content": "hi"}]}, anthropic)
        check("B5", "anthropic budget_tokens=40000 -> 200", "200", str(status))
        check("B5b", "budget 40000 travels as max",
              "max", last().get("reasoning_effort"))

        out.flush()
        err.flush()
        lines = []
        for path in (out_path, err_path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.readlines())
        effort_lines = [ln.rstrip("\n") for ln in lines if "effort_from=" in ln]
        print("\n== log lines ==")
        for line in effort_lines[:12]:
            print("  " + line)
        check("LOG", "log carries effort_from=xhigh effort_to=max", True,
              any("effort_from=xhigh effort_to=max" in ln for ln in effort_lines))

        print("\n== upstream request log ==")
        for row in RECORDS[-12:]:
            print("  " + json.dumps(row, ensure_ascii=False))
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gateway.kill()
        upstream.shutdown()
        out.close()
        err.close()

    print("\n== summary ==")
    for cid, what, expected, actual, verdict in results:
        print("  %-4s %-58s %-20s %-20s %s"
              % (cid, what, expected, actual, verdict))
    failed = [row for row in results if row[4] != "PASS"]
    print("\nPASS=%d FAIL=%d" % (len(results) - len(failed), len(failed)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
