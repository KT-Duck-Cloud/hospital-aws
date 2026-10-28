import socket
import subprocess
import time
import urllib.request


class Clock:
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


class SSHExec:
    """원격 노드의 MySQL 을 SSH + sudo mysql(auth_socket) 로 다룬다. 비밀번호를 명령줄에 싣지 않는다."""

    def __init__(self, user="drops", key="", known=""):
        self.user, self.key, self.known = user, key, known

    def argv(self, host, remote):
        opt = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
        if self.key:
            opt = ["-i", self.key] + opt
        if self.known:
            opt += ["-o", f"UserKnownHostsFile={self.known}"]
        return ["ssh", *opt, f"{self.user}@{host}", remote]

    def run(self, host, remote, data=b"", timeout=600):
        r = subprocess.run(self.argv(host, remote), input=data, capture_output=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).decode("utf-8", "replace").strip()[-400:] or f"exit {r.returncode}")
        return r.stdout

    def sql(self, host, sql, timeout=120):
        return self.run(host, "sudo -n mysql -N --batch", sql.encode(), timeout).decode()

    def script(self, host, text, *args, timeout=1200):
        return self.run(host, "sudo -n bash -s -- " + " ".join(args), text.encode(), timeout).decode()

    def dump(self, host, db, timeout=1200):
        return self.run(host, "sudo -n mysqldump --single-transaction --set-gtid-purged=ON --routines --triggers "
                              f"--events --hex-blob --default-character-set=utf8mb4 --databases {db}", b"", timeout)

    def load(self, host, data, timeout=1200):
        return self.run(host, "sudo -n mysql --default-character-set=utf8mb4", data, timeout)


class Probe:
    """판정 서버 시점의 도달성. 판정 서버가 못 본다고 실제로 죽은 것은 아니다 → 증인(witness)."""

    def tcp_ok(self, host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def app_ready(self, host, port, path="/readyz", timeout=3):
        try:
            with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False
