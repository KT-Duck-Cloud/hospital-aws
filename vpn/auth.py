import base64
import hashlib
import hmac
import secrets

ITERS = 310000
DUMMY = f"pbkdf2_sha256${ITERS}$" + "A" * 24 + "$" + "A" * 44     # 없는 계정도 같은 시간이 걸리게


def hash_password(pw, iters=ITERS):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw, stored):
    try:
        _, iters, salt, dk = stored.split("$")
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(iters)),
                                   base64.b64decode(dk))
    except Exception:
        return False
