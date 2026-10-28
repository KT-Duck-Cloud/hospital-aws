import json
import os
import secrets as _secrets

DR = {"down_since": None, "up_since": None, "active": None, "back_to": None, "fenced": [],
      "failover_try": 0, "failback_try": 0, "app_try": 0, "witness": None, "note": ""}


def _atomic(path, obj, mode=0o644):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


class Store:
    def __init__(self, path=""):
        self.path, self.data = path, {}
        if path:
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                pass
        self.data.setdefault("dr", {})
        for k, v in DR.items():
            self.data["dr"].setdefault(k, v)
        self.data.setdefault("prepared", None)
        self.data.setdefault("repl", {})
        self.data.setdefault("repl_ok", None)
        self.data.setdefault("history", [])
        self.data.setdefault("mode", None)
        self.data.setdefault("site1", [])

    @property
    def dr(self):
        return self.data["dr"]

    def log(self, now, text):
        self.data["history"] = ([{"at": now, "text": text}] + self.data["history"])[:50]

    def save(self):
        if self.path:
            _atomic(self.path, self.data)


class Secrets:
    """복제 계정 비밀번호 — 파일(root 0600)에만. path 가 비면 메모리."""

    def __init__(self, path=""):
        self.path, self.mem = path, {}

    def get(self, user):
        s = self.mem
        if self.path and os.path.exists(self.path):
            with open(self.path) as f:
                s = json.load(f)
        if user not in s:
            s[user] = _secrets.token_hex(15)
            if self.path:
                _atomic(self.path, s, 0o600)
        self.mem = s
        return s[user]
