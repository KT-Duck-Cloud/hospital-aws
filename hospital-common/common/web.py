"""웹 공통 — 세션(쿠키 + CSRF) · 허용 대역 · Origin 확인 · 보안 머리글 · JSON 응답. 표준 라이브러리 http.server 위에서."""
import hmac
import ipaddress
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie

HEADERS = (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
           ("Cache-Control", "no-store"),
           ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"))


class Sessions:
    """tok → {email, csrf, exp, stamp}. stamp_fn(email) 이 바뀌면(비밀번호 변경 · 삭제) 바로 끊긴다."""

    def __init__(self, ttl, stamp_fn=lambda e: None, clock=time.time):
        self.ttl, self.stamp, self.now, self.s, self.lock = ttl, stamp_fn, clock, {}, threading.Lock()

    def new(self, email):
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.s[tok] = {"email": email, "csrf": csrf, "exp": self.now() + self.ttl, "stamp": self.stamp(email)}
        return tok, csrf

    def get(self, tok):
        with self.lock:
            s = self.s.get(tok or "")
            if s and (s["exp"] < self.now() or s["stamp"] != self.stamp(s["email"])):
                self.s.pop(tok, None)
                return None
            return s

    def drop(self, tok):
        with self.lock:
            self.s.pop(tok or "", None)


def cookie(name, tok, ttl, path="/", same="Strict"):
    return ("Set-Cookie", f"{name}={tok}; Path={path}; Max-Age={ttl if tok else 0}; HttpOnly; Secure; SameSite={same}")


def read_cookie(headers, name):
    c = SimpleCookie()
    try:
        c.load(headers.get("Cookie") or "")
    except Exception:
        return None
    return c[name].value if name in c else None


def nets(cidrs):
    return [ipaddress.ip_network(x) for x in cidrs]


def allowed(ip, networks):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in networks)


def same_origin(headers, origin):
    got = headers.get("Origin")
    return got is None or hmac.compare_digest(got.encode(), origin.encode())


def csrf_ok(s, headers):
    return bool(s) and hmac.compare_digest((headers.get("X-CSRF") or "").encode(), s["csrf"].encode())


def send(h, code, body, ctype="application/json; charset=utf-8", headers=()):
    if not isinstance(body, bytes):
        body = (json.dumps(body, ensure_ascii=False, default=str) + "\n").encode()
    h.send_response(code)
    for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body)))) + HEADERS + tuple(headers):
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def body(h, limit=65536):
    n = int(h.headers.get("Content-Length") or 0)
    if n > limit:
        raise ValueError("요청이 너무 큼")
    raw = h.rfile.read(n) if n else b"{}"
    return json.loads(raw or b"{}")
