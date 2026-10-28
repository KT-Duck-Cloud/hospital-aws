"""console — 운영 콘솔. VPN 대역에서만 열리고, 로그인 · CSRF · 감사 기록 뒤에서 다른 서비스를 부른다.

  GET  /                 화면 (index.html)
  GET  /api/me           로그인 여부 · csrf
  GET  /api/state        호스트 · VM (hostagent) · 공개 입구 S1/S2 (entry) · 공개 주소 응답
  GET  /api/jobs         최근 작업 (단계별)
  GET  /api/accounts     공용 계정 목록 (해시 없음)
  GET  /api/audit        최근 감사 기록
  POST /api/login · /api/logout
  POST /api/action       {"action": vm_start | vm_stop | resize | entry_start | entry_stop | account_add | account_remove | account_unlock, ...}
                         X-CSRF 머리글 필수 · Origin 확인 · 결과와 상관없이 감사 기록
"""
import http.server
import os
import secrets
import time

from common import web
from common.auth import Throttle, norm
from services.console import jobs as J

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "hc_console"


class Console:
    def __init__(self, conf, accounts, agent, audit, entry=None, probe=None, clock=time.time, sleep=time.sleep, sync=False):
        self.c, self.acc, self.agent, self.audit, self.entry = conf, accounts, agent, audit, entry
        self.probe, self.now, self.sleep = probe or (lambda url: 0), clock, sleep
        self.jobs, self.throttle = J.Jobs(clock, sync), Throttle(10, 900, clock)
        self.sess = web.Sessions(conf.get("session_minutes", 60) * 60, accounts.stamp, clock)
        self.nets = web.nets(conf.get("allowed_cidrs", ["100.64.0.0/10", "127.0.0.1/32"]))

    def health(self, name):
        url = (self.c.get("health") or {}).get(name)
        return 200 <= self.probe(url) < 400 if url else name in {v["name"] for v in self.agent("vm_list")["vms"] if v["running"]}

    def state(self):
        out, err = {"at": self.now()}, []
        for k, f in (("host", lambda: self.agent("host_status")), ("vms", lambda: self.agent("vm_list")),
                     ("entry", lambda: {"self": self.entry("status"), "peer": self.entry("peer", verb="status")} if self.entry else None)):
            try:
                out[k] = f()
            except Exception as e:
                out[k], _ = None, err.append(f"{k}: {str(e)[:120]}")
        if self.c.get("public_url"):
            out["public"] = self.probe(self.c["public_url"])
        out["errors"] = err
        return out

    def action(self, who, ip, b):
        a, n = b.get("action"), b.get("name")
        start = lambda kind, fn: self.jobs.start(kind, {k: v for k, v in b.items() if k != "action"}, who, fn)  # noqa: E731
        if a in ("vm_start", "vm_stop"):
            return start(a, J.power(self.agent, n, a == "vm_start"))
        if a == "resize":
            return start(a, J.resize(self.agent, self.health, self.sleep, n, b.get("cpus"), b.get("mem_mb"), b.get("gb")))
        if a in ("entry_start", "entry_stop") and self.entry:
            verb = a.split("_")[1]
            return self.entry(verb, by=f"console:{who}") if b.get("where", "self") == "self" else self.entry("peer", verb=verb, by=f"console:{who}")
        if a == "account_add":
            pw = secrets.token_urlsafe(12)
            self.acc.add(b["email"], pw, b.get("note", ""))
            return {"email": norm(b["email"]), "password": pw, "note": "이 화면에서 한 번만 보입니다"}
        if a == "account_remove":
            return {"removed": self.acc.remove(b["email"])}
        if a == "account_unlock":
            return {"unlocked": self.acc.unlock(b["email"])}
        raise ValueError(f"허용되지 않은 동작: {a}")


def handler(con):
    with open(os.path.join(HERE, "index.html"), "rb") as f:
        page = f.read()
    origin = con.c.get("origin", "https://console.example.org")

    class H(http.server.BaseHTTPRequestHandler):
        server_version, sys_version = "console", ""

        def log_message(self, *a):
            pass

        def ip(self):
            return self.client_address[0]

        def me(self):
            return con.sess.get(web.read_cookie(self.headers, COOKIE))

        def do_GET(self):
            if not web.allowed(self.ip(), con.nets):
                return web.send(self, 403, {"error": "허용 대역 아님"})
            p, s = self.path.split("?")[0], self.me()
            if p == "/":
                return web.send(self, 200, page, "text/html; charset=utf-8")
            if p == "/api/me":
                return web.send(self, 200, {"email": s["email"], "csrf": s["csrf"]} if s else {"email": None})
            if not s:
                return web.send(self, 401, {"error": "로그인 필요"})
            get = {"/api/state": con.state, "/api/jobs": lambda: con.jobs.list, "/api/accounts": con.acc.list,
                   "/api/audit": lambda: con.audit.tail(50)}.get(p)
            web.send(self, 200, get()) if get else web.send(self, 404, {"error": "없는 주소"})

        def do_POST(self):
            ip = self.ip()
            if not web.allowed(ip, con.nets) or not web.same_origin(self.headers, origin):
                return web.send(self, 403, {"error": "거부"})
            p = self.path.split("?")[0]
            try:
                b = web.body(self)
            except ValueError as e:
                return web.send(self, 400, {"error": str(e)})
            if p == "/api/login":
                if con.throttle.blocked(ip):
                    return web.send(self, 429, {"error": "잠시 뒤 다시"})
                if con.acc.verify(ip, b.get("email"), b.get("pw")) != "ok":
                    con.throttle.hit(ip)
                    con.audit.write(norm(b.get("email")), ip, "login", {}, "fail")
                    return web.send(self, 401, {"error": "이메일 또는 비밀번호가 맞지 않습니다"})
                tok, csrf = con.sess.new(norm(b["email"]))
                con.audit.write(norm(b["email"]), ip, "login", {}, "ok")
                return web.send(self, 200, {"csrf": csrf}, headers=[web.cookie(COOKIE, tok, con.sess.ttl)])
            s = self.me()
            if not web.csrf_ok(s, self.headers):
                return web.send(self, 403, {"error": "세션 또는 CSRF 확인 실패"})
            if p == "/api/logout":
                con.sess.drop(web.read_cookie(self.headers, COOKIE))
                return web.send(self, 200, {"ok": True}, headers=[web.cookie(COOKIE, "", 0)])
            if p != "/api/action":
                return web.send(self, 404, {"error": "없는 주소"})
            args = {k: v for k, v in b.items() if k not in ("action", "pw")}
            try:
                res = con.action(s["email"], ip, b)
                con.audit.write(s["email"], ip, b.get("action"), args, "ok")
                web.send(self, 200, res)
            except (ValueError, KeyError, RuntimeError) as e:
                con.audit.write(s["email"], ip, b.get("action"), args, f"error: {e}")
                web.send(self, 400, {"error": str(e)})

    return H
