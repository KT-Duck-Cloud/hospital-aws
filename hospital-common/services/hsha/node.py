"""Headscale 한 서버의 상태 사본 — 설정(/etc/headscale) + DB(sqlite .backup, 실행 중에도 일관) + 키 파일을 tar.gz 하나로.

  snapshot()        실행 중에도 → bytes
  restore(blob)     멈춘 상태에서만. 지금 상태를 백업 디렉터리에 남긴 뒤 풀고, WAL · SHM 은 지운다
  receive(blob)     대기 서버가 받은 최신 사본을 보관 (latest.tgz + pushed_at)
"""
import io
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time


class HsNode:
    def __init__(self, role, host, root="/", store=None):
        self.role, self.h, self.root = role, host, root
        self.etc, self.lib = os.path.join(root, "etc/headscale"), os.path.join(root, "var/lib/headscale")
        self.store = store or os.path.join(root, "var/lib/hsha")

    def running(self):
        return self.h.active("headscale")

    def snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "var/lib/headscale"))
            shutil.copytree(self.etc, os.path.join(t, "etc/headscale"))
            src, dst = sqlite3.connect(os.path.join(self.lib, "db.sqlite")), sqlite3.connect(os.path.join(t, "var/lib/headscale/db.sqlite"))
            with dst:
                src.backup(dst)
            src.close(), dst.close()
            for k in (f for f in os.listdir(self.lib) if f.endswith(".key")):
                shutil.copy2(os.path.join(self.lib, k), os.path.join(t, "var/lib/headscale", k))
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(os.path.join(t, "etc"), "etc")
                tar.add(os.path.join(t, "var"), "var")
            return buf.getvalue()

    def restore(self, blob):
        if self.running():
            raise RuntimeError("Headscale 실행 중 — 먼저 멈추기")
        if not blob:
            raise ValueError("빈 사본")
        bak = os.path.join(self.store, f"before-restore-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.h.now()))}")
        for d in (self.etc, self.lib):
            if os.path.isdir(d):
                shutil.copytree(d, os.path.join(bak, os.path.basename(d)), dirs_exist_ok=True)
        for f in ("db.sqlite-wal", "db.sqlite-shm"):
            if os.path.exists(os.path.join(self.lib, f)):
                os.unlink(os.path.join(self.lib, f))
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            bad = [m.name for m in tar.getmembers() if ".." in m.name or not (m.name in ("etc", "var", "var/lib") or
                                                                         m.name.startswith(("etc/headscale", "var/lib/headscale")))]
            if bad:
                raise ValueError(f"사본에 허용되지 않은 경로: {bad[:3]}")
            tar.extractall(self.root, filter="data")
        return {"restored": True, "backup": bak}

    def receive(self, blob, at=None):
        os.makedirs(self.store, exist_ok=True)
        self.h.write(os.path.join(self.store, "pushed_at"), str(at or self.h.now()))
        with open(os.path.join(self.store, "latest.tgz"), "wb") as f:
            f.write(blob)
        return {"bytes": len(blob)}

    def latest(self):
        p = os.path.join(self.store, "latest.tgz")
        if not os.path.exists(p):
            return None, None
        with open(p, "rb") as f:
            return f.read(), float(self.h.read(os.path.join(self.store, "pushed_at")) or 0)
