"""VMware Workstation 호스트 — vmrun · systemd 유닛(duckcloud-vm@<이름>) · .vmx 파일 · vmdk. 에이전트는 이 모양만 안다."""
import json
import os
import re
import shutil
import time

from common.host import Host

ROOT = "/var/lib/vmware/Virtual Machines"
KV = re.compile(r'^\s*([^=#]+?)\s*=\s*"(.*)"\s*$')


def parse_vmx(text):
    return {m.group(1): m.group(2) for m in map(KV.match, text.splitlines()) if m}


def patch_vmx(text, kv):
    lines, seen = text.splitlines(), set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in kv:
            lines[i], _ = f'{k} = "{kv[k]}"', seen.add(k)
    return "\n".join(lines + [f'{k} = "{v}"' for k, v in kv.items() if k not in seen]) + "\n"


class Vmm:
    def __init__(self, root=ROOT, host=None):
        self.root, self.h, self.disk = root, host or Host(), {}

    def vmx(self, name):
        p = os.path.join(self.root, name, f"{name}.vmx")
        if not os.path.isfile(p):
            raise ValueError(f"vmx 없음: {name}")
        return p

    def running(self):
        _, out = self.h.run(["vmrun", "-T", "ws", "list"], 30)
        return {os.path.basename(os.path.dirname(x.strip())) for x in out.decode().splitlines() if x.strip().endswith(".vmx")}

    def spec(self, name):
        x = parse_vmx(self.h.read(self.vmx(name)) or "")
        return {"cpus": int(x.get("numvcpus", 1)), "mem_mb": int(x.get("memsize", 0)), "disk_gb": self.disk_gb(name, x)}

    def disk_gb(self, name, x=None):
        x = x or parse_vmx(self.h.read(self.vmx(name)) or "")
        f = x.get("scsi0:0.fileName")
        if not f:
            return None
        p = os.path.join(self.root, name, f)
        if p not in self.disk:
            _, out = self.h.run(["qemu-img", "info", "--force-share", "--output=json", p], 30)
            try:
                self.disk[p] = json.loads(out)["virtual-size"] // 1024 ** 3
            except Exception:
                return None
        return self.disk[p]

    def set(self, name, kv):
        p = self.vmx(name)
        shutil.copy2(p, f"{p}.bak-{time.strftime('%Y%m%d%H%M%S')}")
        self.h.write(p, patch_vmx(self.h.read(p), kv))

    def start(self, name):
        unit = f"duckcloud-vm@{name}.service"
        if self.h.active(unit):                 # oneshot RemainAfterExit — 게스트에서 꺼져도 active 로 남는다
            self.h.run(["systemctl", "stop", unit], 200)
        self.h.run(["systemctl", "reset-failed", unit], 10)
        return self.h.run(["systemctl", "start", unit], 200)[0] == 0

    def stop(self, name, hard=False):
        if hard:
            return self.h.run(["vmrun", "-T", "ws", "stop", self.vmx(name), "hard"], 60)[0] == 0
        return self.h.run(["systemctl", "stop", f"duckcloud-vm@{name}.service"], 200)[0] == 0

    def grow(self, name, gb):
        x = parse_vmx(self.h.read(self.vmx(name)))
        p = os.path.join(self.root, name, x["scsi0:0.fileName"])
        self.disk.pop(p, None)
        return self.h.run(["vmware-vdiskmanager", "-x", f"{gb}GB", p], 600)[0] == 0

    def host(self):
        def snap():
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            return sum(v), v[3] + v[4]
        (t1, i1), _, (t2, i2) = snap(), time.sleep(0.5), snap()
        with open("/proc/meminfo") as f:
            m = {k: int(v.split()[0]) // 1024 for k, v in (line.split(":", 1) for line in f)}
        du = shutil.disk_usage(self.root)
        return {"cpus": os.cpu_count(), "cpu_percent": round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1),
                "mem_total_mb": m["MemTotal"], "mem_avail_mb": m["MemAvailable"], "disk_free_gb": du.free // 1024 ** 3}
