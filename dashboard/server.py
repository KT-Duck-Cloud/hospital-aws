"""통합 관제 — 나의 파트와 이어진 운영 사이트 여섯 곳과 같은 모양. 화면은 ../sites/<사이트>/*.html.
  GET  /  (Host 가 dr. 으로 시작하면 DR 현황)        duckcloud-stat — 서비스 상태 (디자인 · 화면 동작만, 실시간 값 없음)
  GET  /dr                                         dr — DR 현황, 4초마다 /api/drboard (이것만 실제 상태로 동작)
  GET  /admin  · /admin/{home,dr/live,aws,acl}       duckcloud-admin — 왼쪽 메뉴 + 본문 프레임 (디자인만)
  GET  /master · /master/otp                       duckcloud-admin-master — 과금 · 콘솔 (팀 VPN 전용, 2단계 인증 화면, 디자인만)
  GET  /aws/   · /aws/{aws-services,aws-connection,presentation}   aws — AWS 대시보드 (디자인만)
  GET  /hs/join · /hs/join/{claim,setpw,vm,mesh}    hs — VPN 가입 화면 스냅샷 (실제 동작은 vpn/join.py :8096/join)
  다른 담당 · 운영 서버 전용 화면은 /<사이트>/excluded/... → 포함하지 않음 안내
  GET  /dr/api                                    관리 사이트 DR 칸 — 승격 조건 하나씩 (admin.py)
  POST /acl/api                                   VPN ACL 편집 · 차이 · 경로 시험 (acl.py) — apply 는 acl_apply=True 인 서버만
  /api/aws/* · /api/billing* · /api/otp/* · /api/connection   AWS 대시보드 (cloud/api.py) — 과금 · 전원 제어는 OTP 세션 필수
  GET  /api/drboard                                DR 컨트롤 상태 + 실제 응답 → 화면 모양 (board.py)
  GET  /api/status                                 모은 원자료 {"dr", "vpn", "probes", "phase", "errors"} (5초 캐시)
  POST /api/drill/scenario-N · reset               시연 시나리오 — drill 함수가 있을 때만 (시뮬레이터), X-Dr-Auth 비밀번호
허용된 대역(DASH_ALLOW, 기본 VPN · loopback)에서 온 요청만 받는다."""
import hmac
import http.server
import ipaddress
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from dashboard import acl as ACL
from dashboard import admin, board
from pilotlight.execs import Probe
from vpn import agent as vpn_agent

SITES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sites")
ROUTES = {"/": "duckcloud-stat/index", "/index.html": "duckcloud-stat/index", "/dr": "dr/index",
          "/admin": "duckcloud-admin/index", "/admin/": "duckcloud-admin/index", "/admin/home": "duckcloud-admin/home",
          "/admin/dr/live": "duckcloud-admin/dr-live", "/admin/aws": "duckcloud-admin/aws", "/admin/acl": "duckcloud-admin/acl",
          "/master": "duckcloud-admin-master/index", "/master/": "duckcloud-admin-master/index", "/master/otp": "duckcloud-admin-master/otp",
          "/aws": "aws/index", "/aws/": "aws/index", "/aws/presentation": "aws/index", "/aws/aws-services": "aws/aws-services",
          "/aws/aws-connection": "aws/aws-connection", "/hs/join": "hs/login", "/hs/join/claim": "hs/claim",
          "/hs/join/setpw": "hs/setpw", "/hs/join/vm": "hs/vm", "/hs/join/mesh": "hs/mesh"}
CDN = "https://cdnjs.cloudflare.com https://cdn.tailwindcss.com"
CSP = (f"default-src 'self'; script-src 'self' 'unsafe-inline' {CDN}; style-src 'self' 'unsafe-inline' {CDN}; "
       f"font-src 'self' {CDN} data:; img-src 'self' data:; frame-src 'self'")
EXCLUDED = ("<!doctype html><html lang=ko><head><meta charset=utf-8><title>포함하지 않음</title><style>body{margin:0;background:#0b1220;"
            "color:#cbd5e1;font:14px system-ui,'Apple SD Gothic Neo',sans-serif;display:grid;place-items:center;height:100vh}"
            "div{border:1px dashed #334155;border-radius:12px;padding:24px 28px;text-align:center}b{color:#f59e0b}</style></head>"
            "<body><div><b>{p}</b><br>다른 담당 영역이거나 운영 서버에서만 열리는 화면이라 이 저장소에는 포함하지 않았습니다.</div></body></html>")


def phase(dr):
    d = (dr or {}).get("dr") or {}
    job = (dr or {}).get("job") or {}
    if job.get("running"):
        return {"key": "running", "text": f"실행 중 — {job.get('name')}"}
    if d.get("active"):
        return {"key": "failback", "text": "복귀 대기 — 1조 응답"} if d.get("back_to") else {"key": "active", "text": "AWS 가 1조 대행"}
    if d.get("down_since"):
        return {"key": "down", "text": "1조 장애 감지"}
    return {"key": "normal", "text": "정상 — AWS 대기"} if dr else {"key": "unknown", "text": "DR 컨트롤 응답 없음"}


class Collector:
    def __init__(self, dr_fn, vpn_fn, probe, ttl=5, clock=time.time):
        self.dr_fn, self.vpn_fn, self.probe, self.ttl, self.now = dr_fn, vpn_fn, probe, ttl, clock
        self.cache, self.at, self.lock = None, 0, threading.Lock()

    def probes(self, dr):
        s = (dr or {}).get("sites") or {}
        port = s.get("app_port", 30080)
        out = [{"site": "1조", "kind": "db", "host": h, "ok": self.probe.tcp_ok(h, 3306)} for h in s.get("site1_dbs", [])]
        out += [{"site": "1조", "kind": "app", "host": h, "ok": self.probe.app_ready(h, port)} for h in s.get("site1_apps", [])]
        if s.get("standby_db"):
            out.append({"site": "AWS", "kind": "db", "host": s["standby_db"], "ok": self.probe.tcp_ok(s["standby_db"], 3306)})
        if s.get("standby_app"):
            out.append({"site": "AWS", "kind": "app", "host": s["standby_app"], "ok": self.probe.app_ready(s["standby_app"], port)})
        return out

    def status(self):
        with self.lock:
            if self.cache and self.now() - self.at < self.ttl:
                return self.cache
            errors, dr, vpn = [], None, None
            try:
                dr = self.dr_fn()
            except Exception as e:
                errors.append(f"DR 컨트롤: {str(e)[:120]}")
            try:
                vpn = self.vpn_fn()
            except Exception as e:
                errors.append(f"VPN 에이전트: {str(e)[:120]}")
            self.cache = {"at": self.now(), "phase": phase(dr), "dr": dr, "vpn": vpn, "probes": self.probes(dr), "errors": errors}
            self.at = self.now()
            return self.cache


def http_dr(url, token, timeout=4):
    def fn():
        req = urllib.request.Request(url.rstrip("/") + "/api/state", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    return fn


def vpn_nodes(col):
    vpn = col.status().get("vpn") or {}
    return [{"name": v.get("host"), "ip": v.get("ip"), "user": m.get("user"), "tags": v.get("tags") or []}
            for m in vpn.get("members", []) for v in m.get("vms", [])]


def handler(col, allow, drill=None, password="", cloud=None, policy=None, acl_apply=False):
    pages = {}
    for site in (d for d in os.listdir(SITES) if os.path.isdir(os.path.join(SITES, d))):
        for name in (f for f in os.listdir(os.path.join(SITES, site)) if f.endswith(".html")):
            with open(os.path.join(SITES, site, name), "rb") as f:
                pages[f"{site}/{name[:-5]}"] = f.read()

    class H(http.server.BaseHTTPRequestHandler):
        server_version, sys_version = "pilotlight-dash", ""

        def log_message(self, *a):
            pass

        def reply(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)) or body is None:
                body = (json.dumps(body, ensure_ascii=False, default=str) + "\n").encode()
            self.send_response(code)
            for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                         ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "SAMEORIGIN"),
                         ("Content-Security-Policy", CSP)):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            ip = ipaddress.ip_address(self.client_address[0])
            return any(ip in n for n in allow)

        def send(self, code, body, headers=()):
            data = (json.dumps(body, ensure_ascii=False, default=str) + "\n").encode()
            self.send_response(code)
            for k, v in (("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(data))),
                         ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")) + tuple(headers):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}") if n else {}

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, b"forbidden\n", "text/plain")
            path, _, qs = self.path.partition("?")
            if cloud and cloud.handles(path):
                q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
                return self.send(*cloud.get(path, q, self.headers))
            if path == "/dr/api":
                return self.reply(200, admin.dr_api(col.status()))
            is_dr = (self.headers.get("Host") or "").lower().startswith("dr.")
            if path in ROUTES:
                page = "dr/index" if is_dr and path in ("/", "/index.html") else ROUTES[path]
                return self.reply(200, pages[page], "text/html; charset=utf-8")
            if path.startswith(("/admin/", "/master/", "/aws/", "/hs/")) and not path.endswith("_ping"):
                return self.reply(200, EXCLUDED.replace("{p}", path.split("/excluded", 1)[-1]).encode(), "text/html; charset=utf-8")
            if path.endswith("_ping"):
                return self.reply(200, {"ok": True})
            if path == "/api/drboard":
                st = col.status()
                return self.reply(200 if st["dr"] else 503, board.build(st["dr"], st["probes"]) or {"error": " · ".join(st["errors"])})
            if path == "/api/status":
                return self.reply(200, col.status())
            self.reply(404, b"not found\n", "text/plain")

        def do_POST(self):
            if not self.allowed():
                return self.reply(403, b"forbidden\n", "text/plain")
            path = self.path.split("?")[0]
            origin, host = self.headers.get("Origin"), self.headers.get("Host") or ""
            if origin and urllib.parse.urlparse(origin).netloc != host:
                return self.reply(403, {"success": False, "message": "다른 출처"})
            if (cloud and cloud.handles(path)) or path == "/acl/api":
                try:
                    b = self.body()
                except ValueError:
                    return self.reply(400, {"success": False, "message": "JSON 아님"})
                if path != "/acl/api":
                    return self.send(*cloud.post(path, b, self.headers))
                if not policy:
                    return self.reply(404, {"ok": False, "error": "ACL 편집이 없는 서버"})
                if b.get("op") == "apply" and not acl_apply:
                    return self.reply(403, {"ok": False, "error": "이 서버는 ACL 적용을 하지 않습니다 (보기 · 시험만)"})
                return self.reply(*ACL.api(policy, lambda: vpn_nodes(col), b, "admin"))
            if not path.startswith("/api/drill/") or not drill:
                return self.reply(404, {"success": False, "message": "시연 기능이 없는 서버입니다"})
            got = self.headers.get("X-Dr-Auth") or ""
            if not (password and hmac.compare_digest(got.encode(), password.encode())):
                return self.reply(403, {"success": False, "message": "인증 실패"})
            try:
                ok, out = drill(path.rsplit("/", 1)[1])
                col.at = 0
                self.reply(200, {"success": ok, "output": out})
            except ValueError as e:
                self.reply(400, {"success": False, "message": str(e)})

    return H


def main():
    env = os.environ.get
    allow = [ipaddress.ip_network(x.strip()) for x in env("DASH_ALLOW", "100.64.0.0/10,127.0.0.1/32").split(",") if x.strip()]
    sock = env("VPN_AGENT_RO_SOCK", "/run/pilotlight-vpn-ro/agent.sock")
    col = Collector(http_dr(env("DR_CONTROL_URL", "http://127.0.0.1:8090"), env("DR_CONTROL_TOKEN", "")),
                    lambda: vpn_agent.call(sock, "overview", timeout=10), Probe())
    host, port = env("DASH_LISTEN", "127.0.0.1:8095").rsplit(":", 1)
    print("통합 관제", host, port, flush=True)
    http.server.ThreadingHTTPServer((host, int(port)), handler(col, allow)).serve_forever()


if __name__ == "__main__":
    main()
