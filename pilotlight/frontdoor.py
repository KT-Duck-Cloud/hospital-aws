import ipaddress
import os
import shlex
import subprocess


def render(primary, backup, port):
    for ip in (*primary, *backup):
        ipaddress.IPv4Address(ip)
    if not primary:
        return "    server 127.0.0.1:9;   # 정상 사이트 없음 — 연결 실패 → 안내 페이지\n"
    return ("".join(f"    server {ip}:{port} max_fails=3 fail_timeout=10s;\n" for ip in primary) +
            "".join(f"    server {ip}:{port} backup max_fails=2 fail_timeout=10s;\n" for ip in backup))


class Frontdoor:
    """nginx upstream 파일 하나만 바꾼다. 바뀐 경우에만 교체 → 검사 통과 시 reload, 실패하면 이전 파일로 되돌린다."""

    def __init__(self, path, port, test="nginx -t", reload="systemctl reload nginx", sh=None):
        self.path, self.port, self.test, self.reload = path, port, test, reload
        self.sh = sh or (lambda cmd: subprocess.run(shlex.split(cmd), capture_output=True, timeout=30).returncode == 0)
        self.current = None

    def point(self, primary, backup=()):
        body = "upstream pilotlight_site {\n" + render(primary, backup, self.port) + "}\n"
        if not self.path:
            changed, self.current = body != self.current, body
            return changed
        old = None
        if os.path.exists(self.path):
            with open(self.path) as f:
                old = f.read()
        if old == body:
            return False
        with open(self.path + ".tmp", "w") as f:
            f.write(body)
        os.replace(self.path + ".tmp", self.path)
        if not (self.sh(self.test) and self.sh(self.reload)):
            if old is None:
                os.remove(self.path)
            else:
                with open(self.path, "w") as f:
                    f.write(old)
            raise RuntimeError(f"frontdoor 검사 실패 — 되돌림 ({self.test})")
        self.current = body
        return True
