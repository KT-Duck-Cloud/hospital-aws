"""계정 — 콘솔 · 공개 첫 화면이 같은 계정 DB · 같은 해시 · 같은 잠금을 쓴다. 형식은 여기 한 곳에서만 바꾼다.

  해시     pbkdf2_sha256$<반복>$<salt b64>$<dk b64>  (310,000회)
  잠금     계정 5회 연속 실패 → locked (콘솔에서만 해제) · IP 당 15분 10회 실패 → 거절(429)
  타이밍   없는 계정도 DUMMY 해시로 같은 시간을 쓴다 · 실패 사유는 밖에 알리지 않는다
  세션     pw_at (비밀번호 바뀐 시각) 이 세션과 다르면 끊는다
"""
import base64
import contextlib
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

ITERS, MAX_FAILS = 310000, 5
SCHEMA = """CREATE TABLE IF NOT EXISTS accounts (email TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, note TEXT DEFAULT '',
  created REAL NOT NULL, last_login REAL, fails INTEGER DEFAULT 0, locked INTEGER DEFAULT 0, pw_at REAL);
CREATE TABLE IF NOT EXISTS login_log (at REAL, ip TEXT, email TEXT, result TEXT);"""


def dummy(iters=ITERS):
    return f"pbkdf2_sha256${iters}$" + "A" * 24 + "$" + "A" * 44


def hash_password(pw, iters=ITERS):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw, stored):
    try:
        _, iters, salt, dk = stored.split("$")
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(iters)), base64.b64decode(dk))
    except Exception:
        return False


def norm(email):
    return (email or "").strip().lower()[:200]


class Throttle:
    def __init__(self, limit=10, window=900, clock=time.time):
        self.limit, self.window, self.now, self.hits, self.lock = limit, window, clock, {}, threading.Lock()

    def blocked(self, key):
        with self.lock:
            t = self.now()
            self.hits[key] = [x for x in self.hits.get(key, []) if t - x < self.window]
            return len(self.hits[key]) >= self.limit

    def hit(self, key):
        with self.lock:
            self.hits.setdefault(key, []).append(self.now())


class Accounts:
    def __init__(self, path, iters=ITERS, clock=time.time):
        self.path, self.iters, self.now = path, iters, clock
        with self.con() as c:
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def con(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def row(self, email):
        with self.con() as c:
            return c.execute("SELECT * FROM accounts WHERE email=?", (norm(email),)).fetchone()

    def add(self, email, pw, note=""):
        if len(pw) < 12:
            raise ValueError("비밀번호는 12자 이상")
        with self.con() as c:
            c.execute("INSERT INTO accounts (email, pw_hash, note, created, pw_at) VALUES (?,?,?,?,?)",
                      (norm(email), hash_password(pw, self.iters), note[:100], self.now(), self.now()))

    def set_password(self, email, pw):
        with self.con() as c:
            c.execute("UPDATE accounts SET pw_hash=?, pw_at=?, fails=0, locked=0 WHERE email=?", (hash_password(pw, self.iters), self.now(), norm(email)))

    def remove(self, email):
        with self.con() as c:
            return c.execute("DELETE FROM accounts WHERE email=?", (norm(email),)).rowcount

    def unlock(self, email):
        with self.con() as c:
            return c.execute("UPDATE accounts SET fails=0, locked=0 WHERE email=?", (norm(email),)).rowcount

    def list(self):
        with self.con() as c:
            return [dict(r) for r in c.execute("SELECT email, note, created, last_login, fails, locked FROM accounts ORDER BY email")]

    def stamp(self, email):
        r = self.row(email)
        return r and r["pw_at"]

    def log(self, ip, email, result):
        with self.con() as c:
            c.execute("INSERT INTO login_log VALUES (?,?,?,?)", (self.now(), ip, norm(email), result))

    def verify(self, ip, email, pw):
        """→ "ok" | "bad" | "locked" (밖에는 ok 여부만 보인다)"""
        r = self.row(email)
        good = check_password(pw or "", r["pw_hash"] if r else dummy(self.iters))
        res = "bad" if not r else "locked" if r["locked"] else "ok" if good else "bad"
        with self.con() as c:
            if res == "ok":
                c.execute("UPDATE accounts SET fails=0, last_login=? WHERE email=?", (self.now(), r["email"]))
            elif r and not r["locked"]:
                c.execute("UPDATE accounts SET fails=fails+1, locked=(fails+1>=?) WHERE email=?", (MAX_FAILS, r["email"]))
        self.log(ip, email, res)
        return res
