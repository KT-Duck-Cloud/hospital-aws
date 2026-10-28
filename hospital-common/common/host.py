"""서버 조작 어댑터 — systemd · nginx · 파일 · HTTP 확인. 서비스 코드는 이것만 부르고, 시뮬레이터는 같은 모양의 가짜를 넣는다."""
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request


class Host:
    def __init__(self, name="", clock=time.time):
        self.name, self.now = name, clock

    def run(self, cmd, timeout=120, data=None):
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)

    def active(self, unit):
        return self.run(["systemctl", "is-active", "--quiet", unit], 10)[0] == 0

    def enabled(self, unit):
        return self.run(["systemctl", "is-enabled", "--quiet", unit], 10)[0] == 0

    def start(self, unit):
        """켜고, 재부팅해도 켜진 채"""
        return self.run(["systemctl", "enable", "--now", unit], 200)[0] == 0

    def stop(self, unit):
        """끄고, 재부팅해도 꺼진 채"""
        return self.run(["systemctl", "disable", "--now", unit], 200)[0] == 0

    def read(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write(self, path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def nginx_reload(self):
        return self.run(["nginx", "-t"], 30)[0] == 0 and self.run(["nginx", "-s", "reload"], 30)[0] == 0

    def http(self, url, headers=None, timeout=5, insecure=False):
        """→ (status, body). 연결 실패는 (0, b"")"""
        ctx = ssl._create_unverified_context() if insecure else None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout, context=ctx) as r:
                return r.status, r.read(65536)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception:
            return 0, b""
