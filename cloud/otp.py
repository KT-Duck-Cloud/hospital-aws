"""과금 · 전원 제어용 2단계 인증 — TOTP (RFC 6238, 30초 · 6자리 · 앞뒤 1칸 허용). 통과하면 15분 세션.
비밀(base32)은 코드에 두지 않는다: AWS_OTP_SECRET_FILE (0600, {"user": ..., "secret": ...}) 에서 읽고, 없으면 제어를 모두 거부."""
import base64
import hmac
import json
import os
import re
import secrets
import struct
import threading
import time

TTL = 900


def hotp(secret, counter, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


class Otp:
    def __init__(self, path=None, clock=time.time, secret=None, user="master"):
        self.now, self.sess, self.lock, self.fails = clock, {}, threading.Lock(), []
        self.user, self.secret = user, secret
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.user, self.secret = d.get("user", user), d.get("secret")

    def verify(self, code):
        code = re.sub(r"\D", "", code or "")
        with self.lock:
            t = self.now()
            self.fails = [x for x in self.fails if t - x < 300]
            if len(self.fails) >= 5:
                return False, "5분 동안 5번 틀렸습니다 — 잠시 뒤 다시"
            if not self.secret:
                return False, "서버에 TOTP 비밀이 설정되어 있지 않습니다"
            if len(code) != 6:
                return False, "6자리 숫자를 입력하세요."
            cur = int(t // 30)
            if any(hmac.compare_digest(hotp(self.secret, c), code) for c in (cur - 1, cur, cur + 1)):
                self.sess = {k: v for k, v in self.sess.items() if v > t}
                tok = secrets.token_hex(24)
                self.sess[tok] = t + TTL
                return True, tok
            self.fails.append(t)
            return False, "인증번호가 일치하지 않습니다 (시간 동기화 확인)"

    def token(self, headers):
        tok = headers.get("X-Duck-OTP")
        for part in (headers.get("Cookie") or "").split(";"):
            if part.strip().startswith("duck_aws_otp="):
                tok = tok or part.strip().split("=", 1)[1]
        return tok

    def status(self, headers):
        tok, t = self.token(headers), self.now()
        exp = self.sess.get(tok or "", 0)
        return {"authenticated": exp > t, "user": self.user, "expires_in": int(max(0, exp - t))}

    def check(self, headers, payload):
        if payload.get("otp_code"):
            ok, tok = self.verify(payload["otp_code"])
            if ok:
                return True
        return self.status(headers)["authenticated"]

    def logout(self, headers):
        self.sess.pop(self.token(headers) or "", None)


if __name__ == "__main__":
    import sys
    print(hotp(sys.argv[1], int(time.time() // 30)))
