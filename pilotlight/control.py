"""DR 컨트롤 API — 판정 상태 조회 · 대기 준비 승인 · 모드 전환. Bearer 토큰, 바꾸는 요청은 이력에 남긴다.
  GET  /api/state
  POST /api/action  {"op": "prepare", "force": false} | {"op": "mode", "mode": "auto|off"} | {"op": "tick"}
  POST /api/gateways  점수 서버(hospital-monitoring duck-scorer)의 target 메시지 — 형식이 틀리면 400, 받으면 {"accepted": true}.
                      추천으로 기록만 한다: 승격 · 복귀는 판정 엔진이 자기 확인(타이머 · 복제 · 증인)으로만 한다."""
import hmac
import http.server
import ipaddress
import json
import re
import threading

from . import flows

MAX_BODY = 4096


def state(ctx):
    d = ctx.store.data
    job = ctx.runner.job
    return {"at": ctx.clock.now(), "mode": d.get("mode") or ctx.cfg.mode, "dr": d["dr"], "repl": d["repl"],
            "prepared": d["prepared"], "repl_ok": d["repl_ok"], "site1": d["site1"], "history": d["history"][:30],
            "job": {k: job.get(k) for k in ("name", "running", "ok", "started", "ended", "steps")},
            "frontdoor": ctx.door.current, "scorer": d.get("scorer"),
            "sites": {"site1_dbs": list(ctx.cfg.site1_dbs), "site1_apps": list(ctx.cfg.site1_apps),
                      "standby_db": ctx.cfg.standby_db, "standby_app": ctx.cfg.standby_app, "app_port": ctx.cfg.app_port},
            "rules": {"failover_sec": ctx.cfg.failover_sec, "failback_sec": ctx.cfg.failback_sec,
                      "max_lag_sec": ctx.cfg.max_lag_sec, "interval_sec": ctx.cfg.interval_sec}}


def action(ctx, judge, body, who):
    op = body.get("op")
    if op == "prepare":
        res = flows.prepare(ctx, body.get("source") or None, bool(body.get("force")))
    elif op == "mode":
        m = body.get("mode")
        if m not in ("auto", "off"):
            raise ValueError("mode 는 auto | off")
        ctx.store.data["mode"] = m
        res = {"mode": m}
    elif op == "tick":
        res = {"note": judge.tick()}
    else:
        raise ValueError(f"모르는 동작: {op}")
    ctx.say(f"[control] {who} {op} {json.dumps({k: v for k, v in body.items() if k != 'op'}, ensure_ascii=False)}")
    ctx.store.save()
    return res


HOST = re.compile(r"^t\d{1,2}-vm\d$")


def gateways(ctx, body):
    t, f, ts = body.get("target") or {}, body.get("failed") or {}, body.get("timestamp")
    for x in (t, f):
        if not (isinstance(x, dict) and HOST.match(str(x.get("host", ""))) and ipaddress.ip_address(x.get("ip", "")) in ipaddress.ip_network("100.64.0.0/10")):
            raise ValueError("host · ip 형식 (tN-vmM · 100.64.0.0/10)")
    if t.get("site") not in ("on-prem", "aws") or not isinstance(ts, str) or t["host"][-1] != f["host"][-1]:
        raise ValueError("site 는 on-prem | aws · timestamp 문자열 · 같은 역할(vmN)끼리")
    key, d = f"{f['host']}@{ts}", ctx.store.data
    dup = key in d.setdefault("scorer_seen", [])
    if not dup:
        d["scorer_seen"] = (d["scorer_seen"] + [key])[-50:]
        d["scorer"] = {"target": t, "failed": f, "timestamp": ts, "at": ctx.clock.now()}
        ctx.store.log(ctx.clock.now(), f"점수 서버 추천: {f['host']} 장애 → {t['host']} ({t['site']}) — 판정은 엔진이")
        ctx.store.save()
    return {"accepted": True, "duplicate": dup, "action": "advisory"}


def handler(ctx, judge, token):
    lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        server_version, sys_version = "pilotlight", ""

        def log_message(self, *a):
            pass

        def send(self, code, obj):
            body = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode()
            self.send_response(code)
            for k, v in (("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))),
                         ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            return bool(token) and hmac.compare_digest(got.encode(), token.encode())

        def do_GET(self):
            if not self.authed():
                return self.send(401, {"error": "unauthorized"})
            if self.path.split("?")[0] == "/api/state":
                for _ in range(3):                 # 판정 스레드가 고치는 중이면 다시
                    try:
                        return self.send(200, json.loads(json.dumps(state(ctx), default=str)))
                    except RuntimeError:
                        pass
                return self.send(503, {"error": "busy"})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.authed():
                return self.send(401, {"error": "unauthorized"})
            if self.path not in ("/api/action", "/api/gateways"):
                return self.send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self.send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                with lock:
                    if self.path == "/api/gateways":
                        return self.send(200, gateways(ctx, body))
                    res = action(ctx, judge, body, self.client_address[0])
                self.send(200, {"ok": True, "result": res})
            except (ValueError, json.JSONDecodeError) as e:
                self.send(400, {"ok": False, "error": str(e)})

    return H


def serve(ctx, judge, listen, token):
    host, port = listen.rsplit(":", 1)
    srv = http.server.ThreadingHTTPServer((host, int(port)), handler(ctx, judge, token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
