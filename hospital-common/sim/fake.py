"""가짜 인프라 — 서비스 코드가 부르는 Host · Vmm 과 같은 모양. 파일은 진짜(임시 폴더), systemd · nginx · HTTP · VM 만 흉내.

  Net      주소 → 게이트웨이. 꺼진 게이트웨이 · 끊긴 길(cut)은 연결 실패(0)
  FakeHost systemd 유닛 {active, enabled} · nginx -t (nginx_ok=False 면 실패) · 가상 시계
           http: 게이트 :63728/health (X-Duck-Gate 가 있으면 자기 Headscale 만 답함) · 커넥터 메트릭 · 공개 주소
  FakeVmm  VM {cpus, mem_mb, disk_gb, on} · 호스트 메모리 · 디스크
"""
import re

from common.host import Host


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Net:
    def __init__(self):
        self.addr, self.cut, self.public = {}, set(), {}

    def reach(self, src, url):
        m = re.match(r"https?://([^/:]+)", url)
        gw = self.addr.get(m.group(1)) if m else None
        return gw if gw and gw.up and (src.role, m.group(1)) not in self.cut else None


class FakeHost(Host):
    def __init__(self, role, net, clock, units=()):
        super().__init__(role, clock.now)
        self.role, self.net, self.clock, self.up, self.nginx_ok = role, net, clock, True, True
        self.units = {u: {"active": False, "enabled": False} for u in units}
        self.runs = []

    def run(self, cmd, timeout=120, data=None):
        self.runs.append(cmd)
        return 0, b""

    def active(self, unit):
        return self.up and self.units.get(unit, {}).get("active", False)

    def enabled(self, unit):
        return self.units.get(unit, {}).get("enabled", False)

    def start(self, unit):
        self.units.setdefault(unit, {}).update(active=True, enabled=True)
        return True

    def stop(self, unit):
        self.units.setdefault(unit, {}).update(active=False, enabled=False)
        return True

    def nginx_reload(self):
        return self.nginx_ok

    def boot(self, up=True):
        """전원 — 켜지면 enabled 유닛이 다시 켜진다 (가드가 막는 유닛은 boot_hook 이 판단)"""
        self.up = up
        for u, s in self.units.items():
            s["active"] = up and s["enabled"] and (getattr(self, "boot_hook", None) is None or self.boot_hook(u))

    def http(self, url, headers=None, timeout=5, insecure=False):
        if not self.up:
            return 0, b""
        if url.startswith("http://127.0.0.1:20245/"):
            n = 4 if self.active(self.connector) else 0
            return 200, f'{{"status":200,"readyConnections":{n}}}'.encode()
        if url in self.net.public:
            return self.net.public[url](), b"ok"
        gw = self.net.reach(self, url)
        if not gw:
            return 0, b""
        if url.endswith("/health") and (headers or {}).get("X-Duck-Gate"):
            return (200, b'{"status":"pass"}') if gw.active("headscale") else (502, b"")
        return 404, b""


class FakeVmm:
    def __init__(self, vms, mem_total_mb=32768, disk_free_gb=200):
        self.vms, self.mem_total, self.disk_free = vms, mem_total_mb, disk_free_gb

    def running(self):
        return {n for n, v in self.vms.items() if v["on"]}

    def spec(self, n):
        v = self.vms[n]
        return {"cpus": v["cpus"], "mem_mb": v["mem_mb"], "disk_gb": v["disk_gb"]}

    def disk_gb(self, n):
        return self.vms[n]["disk_gb"]

    def set(self, n, kv):
        self.vms[n].update(cpus=int(kv["numvcpus"]), mem_mb=int(kv["memsize"]))

    def start(self, n):
        self.vms[n]["on"] = True
        return True

    def stop(self, n, hard=False):
        self.vms[n]["on"] = False
        return True

    def grow(self, n, gb):
        self.disk_free -= gb - self.vms[n]["disk_gb"]
        self.vms[n]["disk_gb"] = gb
        return True

    def host(self):
        used = sum(v["mem_mb"] for v in self.vms.values() if v["on"])
        return {"cpus": 16, "cpu_percent": 12.5, "mem_total_mb": self.mem_total, "mem_avail_mb": self.mem_total - used - 2048,
                "disk_free_gb": self.disk_free}
