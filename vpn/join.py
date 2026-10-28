"""VPN 가입 화면 — 권한 없는 계정으로 127.0.0.1 에 열고, TLS 는 앞의 nginx 가 맡는다. Headscale 조작은 agent socket 에 부탁한다.

  첫 등록  명단(roster)의 이메일 + 이름이 맞고 등록 전이면 → 비밀번호 1회 설정 (10분 안에)
  로그인   이메일 + 비밀번호 → 내 VM 상태 · 1회용 가입 키(한 번만 보여 줌) · 주소 고정 · 옛 노드 삭제
  감시     GET /join/targets.json?exporter=node|mysqld|app — Prometheus http_sd (hospital-monitoring 이 읽는다, 인증 없음 · 비밀 없음)
  설치     GET /join/install.sh — 인증 없음, 비밀 없음: curl -fsSL <login>/join/install.sh | sudo bash -s -- t5-vm1
  보호     실패 사유 비공개 · 계정 5회 실패 잠금 · IP 당 15분 10회 실패 → 429 · 없는 계정도 같은 시간 · 세션 8시간(HttpOnly ·
           Secure · SameSite=Strict) · 비밀번호가 바뀌면 세션 끊김 · POST 는 Origin 확인 · 모든 시도 기록
"""
import hmac
import html
import http.server
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
from http.cookies import SimpleCookie

from vpn import agent as _agent
from vpn.auth import DUMMY, check_password, hash_password

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_FAILS, MIN_PW, TTL, COOKIE = 5, 12, 8 * 3600, "pl_vpn"
SCHEMA = """CREATE TABLE IF NOT EXISTS members (email TEXT PRIMARY KEY, name TEXT NOT NULL, slot INTEGER UNIQUE NOT NULL,
  pw_hash TEXT, claimed REAL, last_login REAL, fails INTEGER DEFAULT 0, locked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS log (at REAL, ip TEXT, email TEXT, event TEXT);"""
CSS = """body{font-family:system-ui,-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;background:#0f172a;color:#e2e8f0;margin:0}
main{max-width:760px;margin:0 auto;padding:32px 16px}h1{font-size:20px;margin:0 0 4px}p.sub{color:#94a3b8;margin:0 0 24px;font-size:14px}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px;margin-bottom:16px}
label{display:block;font-size:13px;color:#94a3b8;margin:12px 0 4px}input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:15px}
button{margin-top:16px;padding:10px 16px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;font-size:14px;cursor:pointer}
button.sec{background:#334155}button.warn{background:#b45309}.err{background:#7f1d1d;color:#fecaca;padding:10px;border-radius:8px;font-size:14px}
.ok{background:#14532d;color:#bbf7d0;padding:10px;border-radius:8px;font-size:14px}a{color:#38bdf8}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:8px 6px;border-bottom:1px solid #334155;text-align:left;vertical-align:top}
code,pre{font-family:ui-monospace,Menlo,monospace;font-size:13px}pre{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.on{color:#4ade80}.off{color:#f87171}.muted{color:#94a3b8;font-size:13px}form.inline{display:inline}form.inline button{margin:4px 4px 0 0;padding:6px 10px;font-size:13px}"""
E = html.escape
ROLE = {1: "control-plane + 앱", 2: "worker + MySQL"}
BTN = (("fixip", "주소 고정", "sec"), ("delete", "옛 노드 삭제", "warn"), ("key", "가입 키 발급", "sec"))


def msg(cls, text):
    return f'<p class="{cls}">{E(text)}</p>' if text else ""


def norm_email(s):
    return unicodedata.normalize("NFKC", (s or "").strip().lower())[:254]


def norm_name(s):
    return unicodedata.normalize("NFKC", " ".join((s or "").split()))[:64]


class Site:
    def __init__(self, conf, call=None, clock=time.time):
        self.c, self.now = conf, clock
        self.call = call or (lambda op, **a: _agent.call(conf["agent_sock"], op, **a))
        self.sess, self.claims, self.ipfail, self.lock = {}, {}, {}, threading.Lock()
        con = self.db()
        con.executescript(SCHEMA)
        for m in conf.get("members", []):
            con.execute("INSERT OR IGNORE INTO members(email, name, slot) VALUES(?,?,?)", (norm_email(m["email"]), m["name"], int(m["slot"])))
        con.commit()
        con.close()

    def db(self):
        con = sqlite3.connect(self.c["db"], timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def member(self, email):
        con = self.db()
        try:
            return con.execute("SELECT * FROM members WHERE email=?", (email,)).fetchone()
        finally:
            con.close()

    def event(self, ip, email, ev):
        con = self.db()
        con.execute("INSERT INTO log VALUES(?,?,?,?)", (self.now(), ip, email, ev))
        con.commit()
        con.close()

    def limited(self, ip):
        with self.lock:
            self.ipfail[ip] = [t for t in self.ipfail.get(ip, []) if t > self.now() - 900]
            return len(self.ipfail[ip]) >= 10

    def fail(self, ip):
        with self.lock:
            self.ipfail.setdefault(ip, []).append(self.now())

    # ---------- 화면 (운영 가입 화면과 같은 모양) ----------
    def page(self, title, body, sub="DuckCloud 팀 VPN (Headscale) 가입"):
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta name="robots" content="noindex"><title>DuckCloud VPN 가입</title><style>{CSS}</style></head><body><main>'
                f'<h1>{E(title)}</h1><p class="sub">{E(sub)}</p>{body}</main></body></html>').encode()

    def login_page(self, err=""):
        return self.page("로그인", msg("err", err) + """<div class="card"><form method="post" action="/join/login">
<label>이메일</label><input name="email" type="email" autocomplete="username" required>
<label>비밀번호</label><input name="pw" type="password" autocomplete="current-password" required>
<button>로그인</button></form></div>
<div class="card"><b>처음이신가요?</b><p class="muted">관리자가 등록한 이메일과 이름이 맞으면 비밀번호를 한 번 정하고 바로 시작합니다.</p>
<a href="/join/claim">첫 등록 →</a></div>""")

    def claim_page(self, err=""):
        return self.page("첫 등록", msg("err", err) + """<div class="card"><form method="post" action="/join/claim">
<label>이메일 (관리자에게 알려 준 주소)</label><input name="email" type="email" required>
<label>이름</label><input name="name" required>
<button>확인</button></form><p class="muted">한 번 등록하면 다시 할 수 없습니다. 비밀번호를 잊으면 관리자에게 초기화를 요청하세요.</p></div>
<p><a href="/join">← 로그인</a></p>""")

    def setpw_page(self, tok, name, err=""):
        return self.page("비밀번호 설정", msg("err", err) + f"""<div class="card"><p>{E(name)} 님, 로그인에 쓸 비밀번호를 정하세요 ({MIN_PW}자 이상).</p>
<form method="post" action="/join/setpw"><input type="hidden" name="t" value="{E(tok)}">
<label>비밀번호</label><input name="pw" type="password" minlength="{MIN_PW}" autocomplete="new-password" required>
<label>비밀번호 확인</label><input name="pw2" type="password" minlength="{MIN_PW}" autocomplete="new-password" required>
<button>설정하고 시작</button></form><p class="muted">10분 안에 설정하세요.</p></div>""")

    def home(self, m, note="", key=None, error=""):
        s = m["slot"]
        try:
            st = self.call("member_status", slot=s)
        except Exception as e:
            st, error = {"nodes": [], "protected": []}, error or f"상태 조회 실패: {e}"
        by, prot = {}, set(st.get("protected") or [])
        for n in st["nodes"]:
            if n.get("vm"):
                by.setdefault(n["vm"], []).append(n)
        show = sorted(set([1, 2]) | set(by))
        nxt = next((v for v in range(1, 10) if v not in show), None)
        rows = ""
        for v in show + ([nxt] if nxt else []):
            ns, host, want = by.get(v, []), f"t{s}-vm{v}", f"{self.c.get('prefix', '100.64.0.')}{s}{v}"
            if not ns:
                state, acts = f'<span class="muted">{"가입 전" if v <= 2 or v != nxt else "VM 추가"}</span>', ["key"]
            else:
                n = ns[0]
                state = (f'<span class="{"on" if n["online"] else "off"}">● {"온라인" if n["online"] else "오프라인"}</span><br>'
                         f'<span class="muted">현재 {E(n["ip"] or "-")}{"" if n["ip"] == want else " → 자동 고정 대기"}</span>')
                if len(ns) > 1:
                    state += '<br><span class="off">같은 이름 노드가 여러 개 — 오프라인인 옛 노드를 삭제</span>'
                acts = ([] if n["ip"] == want else ["fixip"]) + ([] if n["online"] else ["delete"]) + ["key"]
            if v in prot:
                acts, state = [], state + '<br><span class="muted">1조 운영 노드 — 상태만 표시</span>'
            forms = "".join(f'<form class="inline" method="post" action="/join/{a}"><input type="hidden" name="vm" value="{v}">'
                            f'<button class="{c}">{t}</button></form>' for a, t, c in BTN if a in acts)
            role = "1조 운영 노드" if v in prot else ROLE.get(v, "worker")
            rows += (f'<tr><td><b>{host}</b><br><span class="muted">VM{v} · {role}</span></td><td><code>{want}</code></td>'
                     f'<td>{state}</td><td>{forms}</td></tr>')
        k = ""
        if key:
            cmd = f"curl -fsSL {E(key['login_server'])}/join/install.sh | sudo bash -s -- {E(key['host'])}"
            k = (f'<div class="card"><b>{E(key["host"])} 가입 키</b> <span class="muted">(1회용 · 1시간 · 이 화면을 벗어나면 다시 볼 수 없음)</span>'
                 f'<pre>{E(key["key"])}</pre><p class="muted">VM(Ubuntu 서버)에서 한 줄 실행 → <b>가입 키:</b> 가 나오면 위 키를 붙여 넣고 Enter</p>'
                 f'<pre>{cmd}</pre><p class="muted">호스트 이름 설정 · tailscale 설치 · 가입 · 주소 고정까지 자동 (1~2분). '
                 f'VPN 으로 SSH 되는 것을 확인한 뒤 방화벽: <code>{cmd} --ufw</code></p></div>')
        return self.page(f"{m['name']} 님의 VM", msg("ok", note) + msg("err", error) + k +
                         f'<div class="card"><table><tr><th>VM</th><th>VPN 주소</th><th>상태</th><th></th></tr>{rows}</table>'
                         '<p class="muted">VM 은 1~9대. 맨 아래 줄로 하나씩 추가합니다.</p></div>'
                         '<form method="post" action="/join/logout"><button class="sec">로그아웃</button></form>',
                         f"{m['email']} · 팀원 {s}번 · Headscale {self.c.get('login_server', '')}")

    # ---------- 세션 ----------
    def session(self, cookie):
        c = SimpleCookie(cookie or "")
        tok = c[COOKIE].value if COOKIE in c else ""
        with self.lock:
            s = self.sess.get(tok)
        if not s or s["exp"] < self.now():
            return None, tok
        m = self.member(s["email"])
        if not m or m["pw_hash"] != s["pw_hash"] or m["locked"]:
            return None, tok
        return m, tok

    def new_session(self, m):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.sess[tok] = {"email": m["email"], "pw_hash": m["pw_hash"], "exp": self.now() + TTL}
        return ("Set-Cookie", f"{COOKIE}={tok}; Path=/join; Max-Age={TTL}; HttpOnly; Secure; SameSite=Strict")

    # ---------- 처리 ----------
    def targets(self, exporter):
        """Prometheus http_sd (모니터링 저장소가 읽는다) — 지금 VPN 에 붙은 노드만. 이름 · 주소 · 조만, 개인정보 없음"""
        port, vm = {"node": (9100, None), "mysqld": (9104, 2), "app": (30800, 1)}.get(exporter, (None, None))
        if not port:
            raise ValueError("exporter 는 node | mysqld | app")
        ov = self.call("overview")
        return [{"targets": [f"{v['ip']}:{port}"], "labels": {"team": m["user"], "host": v["host"], "vm": f"vm{v['vm']}"}}
                for m in ov["members"] for v in m["vms"] if v["online"] and v["ip_ok"] and (vm is None or v["vm"] == vm)]

    def get(self, path, cookie, query=""):
        if path == "/join/targets.json":
            try:
                ex = urllib.parse.parse_qs(query).get("exporter", ["node"])[0]
                return 200, json.dumps(self.targets(ex)).encode(), "application/json", ()
            except (ValueError, RuntimeError, OSError) as e:
                return 400, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json", ()
        if path == "/join":
            m, _ = self.session(cookie)
            return 200, self.home(m) if m else self.login_page(), "text/html; charset=utf-8", ()
        if path == "/join/claim":
            return 200, self.claim_page(), "text/html; charset=utf-8", ()
        if path == "/join/install.sh":
            with open(os.path.join(HERE, "install.sh"), "rb") as f:
                body = f.read().replace(b"__LOGIN_SERVER__", self.c["login_server"].encode())
            return 200, body, "text/x-shellscript; charset=utf-8", ()
        if path == "/join/dr.pub" and self.c.get("dr_pub"):
            with open(self.c["dr_pub"], "rb") as f:
                return 200, f.read(), "text/plain; charset=utf-8", ()
        return 404, self.page("없는 주소", "<a href=/join>처음으로</a>"), "text/html; charset=utf-8", ()

    def post(self, path, f, ip, cookie):
        if path in ("/join/login", "/join/claim", "/join/setpw"):
            if self.limited(ip):
                self.event(ip, norm_email(f.get("email")), f"{path} 429")
                return 429, self.page("잠시 후 다시", "<p class=err>실패가 많아 15분 동안 막혔습니다.</p>"), ()
            return getattr(self, path.rsplit("/", 1)[1])(ip, f)
        m, tok = self.session(cookie)
        if not m:
            return 303, b"", (("Location", "/join"),)
        if path == "/join/logout":
            with self.lock:
                self.sess.pop(tok, None)
            return 303, b"", (("Location", "/join"), ("Set-Cookie", f"{COOKIE}=; Path=/join; Max-Age=0; HttpOnly; Secure; SameSite=Strict"))
        try:
            vm, s = int(f.get("vm", 0)), m["slot"]
            if not 1 <= vm <= 9:
                raise ValueError("vm 은 1~9")
            host = f"t{s}-vm{vm}"
            if path == "/join/key":
                k = self.call("join_key", slot=s)
                self.event(ip, m["email"], f"key {host}")
                return 200, self.home(m, key={**k, "host": host}), ()
            if path == "/join/fixip":
                r = self.call("fix_ip", slot=s, vm=vm)
                self.event(ip, m["email"], f"fixip {host} {r}")
                return 200, self.home(m, f"{host} → {r['ip']}" + (" 고정. VM 에서 systemctl restart tailscaled" if r["changed"] else " 이미 맞음")), ()
            if path == "/join/delete":
                r = self.call("node_delete", slot=s, vm=vm)
                self.event(ip, m["email"], f"delete {host} {r}")
                return 200, self.home(m, f"{host} 옛 노드 {r['deleted']}개 삭제 — 새 가입 키로 다시 가입"), ()
        except Exception as e:
            self.event(ip, m["email"], f"{path} error {e}")
            return 200, self.home(m, error=str(e)), ()
        return 404, self.page("없는 주소", ""), ()

    def login(self, ip, f):
        email, pw = norm_email(f.get("email")), (f.get("pw") or "")[:256]
        m = self.member(email)
        ok = check_password(pw, m["pw_hash"] if m and m["pw_hash"] else DUMMY)
        if not (m and m["pw_hash"] and not m["locked"] and ok):
            self.fail(ip)
            if m and m["pw_hash"] and not m["locked"]:
                con = self.db()
                con.execute("UPDATE members SET fails=fails+1, locked=CASE WHEN fails+1>=? THEN 1 ELSE 0 END WHERE email=?", (MAX_FAILS, email))
                con.commit()
                con.close()
            self.event(ip, email, "login fail")
            return 401, self.login_page("이메일 또는 비밀번호가 맞지 않거나 사용할 수 없는 계정입니다."), ()
        con = self.db()
        con.execute("UPDATE members SET fails=0, last_login=? WHERE email=?", (self.now(), email))
        con.commit()
        con.close()
        self.event(ip, email, "login ok")
        return 303, b"", (("Location", "/join"), self.new_session(m))

    def claim(self, ip, f):
        email, name = norm_email(f.get("email")), norm_name(f.get("name"))
        m = self.member(email)
        if not (m and hmac.compare_digest(norm_name(m["name"]).encode(), name.encode()) and not m["pw_hash"] and not m["locked"]):
            self.fail(ip)
            self.event(ip, email, "claim fail")
            return 401, self.claim_page("일치하는 등록 정보가 없거나 이미 등록되었습니다."), ()
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.claims[tok] = {"email": email, "exp": self.now() + 600}
        self.event(ip, email, "claim match")
        return 200, self.setpw_page(tok, m["name"]), ()

    def setpw(self, ip, f):
        tok = f.get("t") or ""
        with self.lock:
            c = self.claims.get(tok)
        if not c or c["exp"] < self.now():
            self.fail(ip)
            return 401, self.claim_page("시간이 지났습니다. 처음부터 다시 하세요."), ()
        m = self.member(c["email"])
        pw, pw2 = (f.get("pw") or "")[:256], (f.get("pw2") or "")[:256]
        if len(pw) < MIN_PW or pw != pw2:
            return 400, self.setpw_page(tok, m["name"], f"{MIN_PW}자 이상, 두 칸이 같아야 합니다."), ()
        con = self.db()
        cur = con.execute("UPDATE members SET pw_hash=?, claimed=?, fails=0 WHERE email=? AND pw_hash IS NULL",
                          (hash_password(pw), self.now(), c["email"]))
        con.commit()
        con.close()
        with self.lock:
            self.claims.pop(tok, None)
        if cur.rowcount != 1:
            return 409, self.claim_page("이미 등록되었습니다."), ()
        self.event(ip, c["email"], "claim done")
        return 303, b"", (("Location", "/join"), self.new_session(self.member(c["email"])))


def handler(site):
    class H(http.server.BaseHTTPRequestHandler):
        server_version, sys_version = "vpnjoin", ""

        def log_message(self, *a):
            pass

        def reply(self, code, body, ctype="text/html; charset=utf-8", headers=()):
            self.send_response(code)
            for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                         ("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
                         ("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"),
                         *headers):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def ip(self):
            return (self.headers.get("X-Real-IP") or self.client_address[0])[:64]

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            code, body, ctype, hdr = site.get(u.path.rstrip("/"), self.headers.get("Cookie"), u.query)
            self.reply(code, body, ctype, hdr)

        def do_POST(self):
            o = self.headers.get("Origin")
            if not (o is None or o == site.c["public_origin"] or (o == "null" and self.headers.get("Sec-Fetch-Site") == "same-origin")):
                return self.reply(403, site.page("거부", "<p class=err>다른 사이트에서 온 요청입니다.</p>"))
            n = min(int(self.headers.get("Content-Length") or 0), 8192)
            f = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            code, body, hdr = site.post(urllib.parse.urlsplit(self.path).path.rstrip("/"), f, self.ip(), self.headers.get("Cookie"))
            self.reply(code, body, headers=hdr)

    return H


def main():
    with open(os.environ.get("VPN_JOIN_CONF", "/etc/pilotlight/join.json")) as f:
        conf = json.load(f)
    site = Site(conf)
    host, port = conf.get("listen", "127.0.0.1:8096").rsplit(":", 1)
    print("VPN 가입 화면", host, port, flush=True)
    http.server.ThreadingHTTPServer((host, int(port)), handler(site)).serve_forever()


if __name__ == "__main__":
    main()
