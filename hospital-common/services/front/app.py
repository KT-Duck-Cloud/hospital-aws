"""front — 공개 첫 화면. 공개라 내부 주소(IP) · 토큰은 보이지 않는다. 로그인하면 같은 내용을 주소와 함께 본다.

  GET  /                 첫 화면 — 병원 사이트 바로가기 · 지금 요청이 가는 곳 · 공개 입구 S1/S2 · VPN 제어 서버
  GET  /front/connect    로그인 (콘솔과 같은 공용 계정 · 같은 실패 잠금)
  GET  /front/status     로그인 후 — 위 내용 + 주소 · 메쉬 · Headscale 사본 시각
  POST /front/connect · /front/logout     Origin 확인 · 세션 8시간 (HttpOnly · Secure · SameSite=Lax)
지금 요청이 가는 곳 = 공개 입구 nginx 의 upstream hospital_active (DR 컨트롤이 승격 · 복귀 때 바꾼다 — 여기서는 읽기만).
"""
import html
import http.server
import re
import time
import urllib.parse

from common import web
from common.auth import Throttle, norm

COOKIE, TTL = "hc_front", 8 * 3600
CSS = """body{font-family:system-ui,-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;background:#0f172a;color:#e2e8f0;margin:0}
main{max-width:760px;margin:0 auto;padding:32px 16px}h1{font-size:20px;margin:0 0 4px}p.sub{color:#94a3b8;margin:0 0 24px;font-size:14px}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px;margin-bottom:16px}a{color:#38bdf8}
label{display:block;font-size:13px;color:#94a3b8;margin:12px 0 4px}input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:15px}
button{margin-top:16px;padding:10px 16px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;font-size:14px;cursor:pointer}button.sec{background:#334155}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:8px 6px;border-bottom:1px solid #334155;text-align:left}
.on{color:#4ade80}.off{color:#f87171}.muted{color:#94a3b8;font-size:13px}.err{background:#7f1d1d;color:#fecaca;padding:10px;border-radius:8px;font-size:14px}"""
E = html.escape


def serving(text, labels):
    """frontdoor 설정 → (이름, [주소]) — upstream 의 첫 server 가 지금 받는 곳"""
    m = re.search(r"upstream hospital_active \{(.*?)\}", text or "", re.S)
    ips = re.findall(r"server (\d+\.\d+\.\d+\.\d+):\d+", m.group(1)) if m else []
    return (labels.get(ips[0], "알 수 없음") if ips else "확인 안 됨"), ips


class Front:
    def __init__(self, conf, accounts, read_frontdoor, entry=None, hs=None, clock=time.time):
        self.c, self.acc, self.read, self.entry, self.hs, self.now = conf, accounts, read_frontdoor, entry, hs, clock
        self.sess, self.throttle = web.Sessions(TTL, accounts.stamp, clock), Throttle(10, 900, clock)

    def view(self):
        name, ips = serving(self.read(), self.c.get("labels") or {})
        ent = {}
        if self.entry:
            for k, f in (("self", lambda: self.entry("status")), ("peer", lambda: self.entry("peer", verb="status"))):
                try:
                    ent[k] = f()
                except Exception:
                    ent[k] = {}
        return {"site": name, "ips": ips, "entries": ent, "hs": self.hs() if self.hs else {}}

    def page(self, title, body, sub="DuckCloud 병원 서비스"):
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{E(title)}</title><style>{CSS}</style></head><body><main><h1>{E(title)}</h1><p class="sub">{E(sub)}</p>{body}</main></body></html>').encode()

    def cards(self, v, detail):
        rows = ""
        for k in ("self", "peer"):
            x = v["entries"].get(k) or {}
            if not x:
                continue
            ok = x.get("connector_ready")
            rows += (f'<tr><td><b>{E(x.get("role", "?"))}</b></td><td class="{"on" if ok else "off"}">{"● 받는 중" if ok else "○ 꺼짐"}</td>'
                     f'<td class="muted">{("메쉬 " + ("참여" if x.get("mesh") else "빠짐") + " · 연결 " + str(x.get("connections", 0))) if detail else ""}</td>'
                     f'<td>{"VPN 제어 서버" if x.get("headscale") else ""}</td></tr>')
        hs = v["hs"]
        snap = (f'<p class="muted">Headscale 사본 {int(self.now() - hs["snapshot_at"])}초 전</p>' if detail and hs.get("snapshot_at") else "")
        where = E(v["site"]) + (f' <code class="muted">{E(" · ".join(v["ips"]))}</code>' if detail else "")
        return (f'<div class="card"><b>지금 요청이 가는 곳</b><p>{where}</p><a href="{E(self.c.get("site_url", "/"))}">병원 사이트 열기 →</a></div>'
                f'<div class="card"><b>공개 입구</b><table>{rows or "<tr><td class=muted>확인 안 됨</td></tr>"}</table>{snap}</div>')

    def login_page(self, err=""):
        return self.page("연결", (f'<p class="err">{E(err)}</p>' if err else "") +
                         '<div class="card"><form method="post" action="/front/connect"><label>이메일</label><input name="email" type="email" autocomplete="username" required>'
                         '<label>비밀번호</label><input name="pw" type="password" autocomplete="current-password" required><button>로그인</button></form></div>'
                         '<p><a href="/">← 첫 화면</a></p>')


def handler(fr):
    origin = fr.c.get("origin", "https://www.example.org")

    class H(http.server.BaseHTTPRequestHandler):
        server_version, sys_version = "front", ""

        def log_message(self, *a):
            pass

        def html(self, code, body, headers=()):
            web.send(self, code, body, "text/html; charset=utf-8", headers)

        def go(self, where, headers=()):
            web.send(self, 303, b"", "text/plain", (("Location", where),) + tuple(headers))

        def do_GET(self):
            p, s = self.path.split("?")[0], fr.sess.get(web.read_cookie(self.headers, COOKIE))
            if p == "/":
                return self.html(200, fr.page("DuckCloud", fr.cards(fr.view(), False) +
                                              f'<p><a href="{"/front/status" if s else "/front/connect"}">연결 (팀원) →</a></p>'))
            if p == "/front/connect":
                return self.go("/front/status") if s else self.html(200, fr.login_page())
            if p == "/front/status":
                if not s:
                    return self.go("/front/connect")
                return self.html(200, fr.page("운영 현황", fr.cards(fr.view(), True) +
                                              '<form method="post" action="/front/logout"><button class="sec">로그아웃</button></form>', f'{s["email"]} · 새로 고치면 갱신'))
            self.html(404, fr.page("없는 주소", '<a href="/">첫 화면</a>'))

        def do_POST(self):
            ip = self.client_address[0]
            if not web.same_origin(self.headers, origin):
                return self.html(403, fr.page("거부", ""))
            n = int(self.headers.get("Content-Length") or 0)
            f = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(min(n, 8192)).decode(errors="replace")).items()}
            p = self.path.split("?")[0]
            if p == "/front/logout":
                fr.sess.drop(web.read_cookie(self.headers, COOKIE))
                return self.go("/", [web.cookie(COOKIE, "", 0, same="Lax")])
            if p != "/front/connect":
                return self.html(404, fr.page("없는 주소", ""))
            if fr.throttle.blocked(ip):
                return self.html(429, fr.login_page("잠시 뒤 다시 시도하세요"))
            if fr.acc.verify(ip, f.get("email"), f.get("pw")) != "ok":
                fr.throttle.hit(ip)
                return self.html(401, fr.login_page("이메일 또는 비밀번호가 맞지 않습니다"))
            tok, _ = fr.sess.new(norm(f["email"]))
            self.go("/front/status", [web.cookie(COOKIE, tok, TTL, same="Lax")])

    return H
